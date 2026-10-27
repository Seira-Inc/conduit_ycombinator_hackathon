from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path
from urllib.parse import quote

from jsonschema import Draft202012Validator

from .browser import BrowserSession
from .models import Manifest
from .net import NetworkPolicy, auth_values, fetch
from .session_lock import SessionLease


class InvalidArguments(ValueError):
    def __init__(self, field: str, constraint: str):
        self.field, self.constraint = field, constraint
        super().__init__(f"Invalid {field}: {constraint} constraint failed")


class ToolUnavailable(ValueError):
    """The tool exists but must not run: unverified, changed since verification, or read-only mode."""


def scalar(value):
    if isinstance(value, bool):
        return str(value).lower()
    if value is None:
        return ""
    return str(value)


class Runtime:
    def __init__(self, manifest: Manifest, home: Path, *, read_only=False, headed=False):
        self.manifest, self.home, self.read_only = manifest, home, read_only
        self.browser = BrowserSession(manifest, home, headed=headed) if manifest.kind == "website" else None
        self.lock = asyncio.Lock()
        self.session_lock = None
        self.progress = None
        self._lease_task = None

    async def acquire_browser(self):
        """Hold the connector's browser lease. A lease another holder took while this one was idle
        means handing the browser over: close it without saving and start again, or report busy.
        A session file rewritten by a sign-in elsewhere is loaded by restarting the context."""
        if not self.browser:
            return
        if self.session_lock is not None and not self.session_lock.renew():
            await self.close(save=False)
        if self.session_lock is None:
            lease = SessionLease(self.home / "locks" / (self.manifest.id + ".lock"))
            lease.acquire()
            self.session_lock = lease
        if self.browser.session_changed_on_disk() and not self.browser.manual_login:
            await self.browser.close()
        if self._lease_task is None or self._lease_task.done():
            self._lease_task = asyncio.create_task(self._keep_lease())

    async def _keep_lease(self):
        # Renew only while something runs on this runtime: an idle browser lets its lease lapse so a
        # sign-in in another process can take the session over.
        while self.session_lock is not None:
            await asyncio.sleep(self.session_lock.ttl / 5)
            if self.session_lock is not None and self.lock.locked():
                self.session_lock.renew()

    async def call(self, name: str, arguments: dict, progress=None):
        async with self.lock:
            self.progress = progress
            try:
                return await self._call(name, arguments)
            finally:
                self.progress = None
                if self.session_lock is not None and self.session_lock.held:
                    self.session_lock.renew()

    async def _call(self, name: str, arguments: dict):
        tool = next((t for t in self.manifest.tools if t.name == name), None)
        if tool is None:
            raise ValueError("Unknown tool: " + name)
        errors = list(Draft202012Validator(tool.input_schema).iter_errors(arguments))
        if errors:
            error = errors[0]
            location = ".".join(map(str, error.path))
            if not location and error.validator == "required":
                missing = re.match(r"'([^']+)' is a required property", error.message)
                location = missing.group(1) if missing else ""
            raise InvalidArguments(location or "arguments", str(error.validator))
        if self.read_only and not tool.read_only:
            raise ToolUnavailable(
                "This server was started in read-only mode. Restart without --read-only to enable actions."
            )
        if tool.adapter == "program":
            from .operation_import import verified_program
            from .programs import ProgramError, fingerprint
            from .verifier import FamilyExecutor

            if not verified_program(tool):
                raise ToolUnavailable("This operation has not passed its required verification")
            if fingerprint(tool.config["family"]) != tool.config.get("fingerprint"):
                raise ToolUnavailable("The compiled program changed; recompile and verify before running it")
            await self.acquire_browser()
            try:
                return await FamilyExecutor(self.browser, self.manifest, tool.config["family"]).call(
                    tool.operation, arguments)
            except ProgramError as exc:
                for resource in self.manifest.capabilities:
                    if resource.get("resource") == tool.resource:
                        resource["last_error"] = {"code": exc.code, "uncertain": exc.uncertain}
                raise
        if tool.adapter == "agent":
            from .agent import run_task

            return await run_task(
                self, tool.config["goal"], arguments["details"], arguments.get("max_steps", 18)
            )
        if tool.adapter == "browser":
            if tool.operation == "close":
                await self.close()
                return {"closed": True}
            await self.acquire_browser()
            try:
                result = await self.browser.execute(tool.operation, arguments, tool.config)
            except Exception:
                if self.browser.context is None and self.session_lock:
                    self.session_lock.release()
                    self.session_lock = None
                raise
            if not self.browser.manual_login:
                try:
                    saved = await self.browser.save()
                except Exception:
                    saved = True
                    result["session_warning"] = (
                        "The action ran, but local browser state could not be saved. Keep this session open and run Conduit under your normal signed-in Windows account."
                    )
                if not saved and self.browser.last_save_refusal:
                    result["session_warning"] = (
                        "The page looks signed out (" + self.browser.last_save_refusal
                        + "); the stored session was kept as it was. Sign in again with `conduit login`."
                    )
            return result
        if tool.adapter == "desktop":
            from .desktop import bridge

            return await bridge(tool.operation, self.manifest.desktop, arguments)
        config = tool.config
        headers, auth_query, cookies = auth_values(config.get("security", []))
        query = []
        path = config["path"]
        for parameter in config["bindings"]:
            key, name, location = parameter["key"], parameter["name"], parameter["in"]
            if key not in arguments:
                continue
            value = arguments[key]
            if isinstance(value, list):
                if location == "query" and parameter.get("explode"):
                    query.extend((name, scalar(x)) for x in value)
                    continue
                value = ",".join(map(scalar, value))
            else:
                value = scalar(value)
            if location == "path":
                if value in (".", ".."):
                    raise ValueError("Dot segments are not valid path arguments")
                path = path.replace("{" + name + "}", quote(value, safe=""))
            elif location == "query":
                query.append((name, value))
            elif location == "header":
                headers[name] = value
            else:
                cookies[name] = value
        # Credentials win over user-provided parameters with the same name.
        query = [(name, value) for name, value in query if name not in auth_query]
        query.extend(auth_query.items())
        if cookies:
            headers["Cookie"] = "; ".join(f"{quote(k)}={quote(v)}" for k, v in cookies.items())
        policy = NetworkPolicy(self.manifest.allowed_origins, self.manifest.allow_private)
        result = await fetch(
            policy,
            config["base"] + path,
            method=config["method"],
            headers=headers,
            params=query,
            json_body=arguments.get(config.get("body_key")),
        )
        # A response URL may contain an API-key query parameter; it is never returned.
        result.pop("url", None)
        if result["status"] >= 400:
            raise ValueError(
                f"The API returned HTTP {result['status']}. Check credentials, access, and arguments."
            )
        if isinstance(result["body"], str):
            result["body"] = result["body"][:50000]
        return result

    async def close(self, *, save=True):
        try:
            if self._lease_task is not None:
                self._lease_task.cancel()
                self._lease_task = None
            if self.browser:
                if save and self.browser.context and not self.browser.manual_login:
                    try:
                        await self.browser.save()
                    except Exception:
                        print("Conduit: browser state could not be saved on close.", file=sys.stderr)
                await self.browser.close()
        finally:
            if self.session_lock:
                self.session_lock.release()
                self.session_lock = None
