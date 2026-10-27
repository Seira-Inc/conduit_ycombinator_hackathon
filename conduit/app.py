from __future__ import annotations

import asyncio
import io
import os
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .agent import model_settings
from .audit import Audit, ErrorLog
from .browser import BrowserSession
from .compiler import compile_connector, repair_connector
from .discovery import discover, refresh_forms
from .gateway import build_gateway, mount_gateway
from .generator import Store
from .keep import daily_check, seconds_until
from .login import confirm, fingerprint
from .models import BuildRequest, StrictModel
from .programs import ProgramError
from .registry import Registry
from .runtime import Runtime
from .session_lock import SessionBusy
from .tool_results import error_payload, safe_message
from .users import Users

STATIC = Path(__file__).with_name("static")


class CallRequest(StrictModel):
    tool: str
    arguments: dict = Field(default_factory=dict)


def create_app(home: Path | None = None, *, mcp_json_response: bool = False) -> FastAPI:
    store = Store(home or Path(os.environ.get("CONDUIT_HOME", ".conduit")))
    registry = Registry(store)
    audit = Audit(store.home / "audit.jsonl")
    errors = ErrorLog(store.home / "errors.jsonl")
    users = Users(store.home / "users.json")
    runtimes, jobs, tasks, job_tasks = {}, {}, set(), {}
    build_slots = asyncio.Semaphore(2)
    compile_jobs = {}

    @asynccontextmanager
    async def lifespan(app):
        errors.prune()
        clock = os.environ.get("CONDUIT_DAILY_CHECK", "07:00")
        if clock != "off":
            task = asyncio.create_task(daily_loop(clock))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
        async with manager.run():
            yield
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*(r.close() for r in runtimes.values()), return_exceptions=True)

    app = FastAPI(title="Conduit", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"])
    app.state.store = store
    app.state.registry = registry
    app.state.audit = audit
    app.state.errors = errors
    app.state.users = users
    app.state.runtimes = runtimes
    app.state.jobs = jobs
    app.state.compile_jobs = compile_jobs

    @app.middleware("http")
    async def local_requests(request: Request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin_header = request.headers.get("origin")
            expected = f"{request.url.scheme}://{request.headers.get('host')}"
            # The MCP endpoint has no dashboard page behind it; the SDK's host and origin checks
            # guard it instead of the local header. A foreign Origin is still refused here.
            local = request.headers.get("x-conduit-local") == "1" or request.url.path == "/mcp"
            if (origin_header and origin_header != expected) or not local:
                return JSONResponse(
                    {"detail": "This action must originate from the local Conduit dashboard."},
                    status_code=403,
                )
            if int(request.headers.get("content-length", "0")) > 200000:
                return JSONResponse({"detail": "Request is too large"}, status_code=413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        )
        return response

    def manifest_or_404(connector_id):
        try:
            return store.get(connector_id)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(404, "Connector not found") from exc

    def manifest_mtime(connector_id):
        try:
            return (store.directory(connector_id) / "connector.json").stat().st_mtime_ns
        except (OSError, ValueError):
            return None

    def runtime_for(connector_id):
        runtime = runtimes.get(connector_id)
        mtime = manifest_mtime(connector_id)
        # `conduit login` and `conduit compile` rewrite connector.json from another process. A cached
        # runtime would keep executing the old program; reload it once nothing is running on it.
        stale = (runtime is not None and getattr(runtime, "manifest_mtime", mtime) != mtime
                 and not runtime.lock.locked() and (runtime.browser is None or runtime.browser.context is None))
        if runtime is None or stale:
            runtime = runtimes[connector_id] = Runtime(manifest_or_404(connector_id), store.home)
            runtime.manifest_mtime = mtime
        return runtime

    def prune_jobs():
        for old_id in list(jobs):
            if len(jobs) < 100:
                break
            if jobs[old_id]["status"] in ("complete", "error", "cancelled"):
                del jobs[old_id]

    def start_compilation(connector_id, brief=None, *, resource=None):
        """Learn the whole site, or with `resource` repair one family through the same gates."""
        if connector_id in compile_jobs:
            return {"job_id": compile_jobs[connector_id]}
        if len(tasks) >= 6:
            raise HTTPException(429, "Several operations are running. Wait for one to finish.")
        runtime = runtime_for(connector_id)
        if not runtime.browser:
            raise HTTPException(400, "CRUD compilation currently targets websites")
        job_id = uuid4().hex
        prune_jobs()
        jobs[job_id] = {"id": job_id, "status": "queued", "connector_id": connector_id,
                        "kind": "repair" if resource else "compile", "resource": resource,
                        "messages": [f"Repairing {resource}" if resource else "Preparing CRUD learning and tests"]}
        compile_jobs[connector_id] = job_id
        async def worker():
            try:
                async with build_slots:
                    jobs[job_id]["status"] = "running"
                    async def progress(message):
                        jobs[job_id]["messages"].append(message)
                    async with asyncio.timeout(45 * 60):
                        if resource:
                            result = await repair_connector(runtime, resource, progress=progress, brief=brief)
                        else:
                            result = await compile_connector(runtime, progress=progress, brief=brief)
                    store.write(runtime.manifest)
                    runtime.manifest_mtime = manifest_mtime(connector_id)
                    jobs[job_id].update(status="complete", result=result)
                    await gateway.notify_tools_changed()
            except asyncio.CancelledError:
                runtime.manifest.compilation.update(status="incomplete", message="Learning stopped; inspect any test objects listed for cleanup.")
                store.write(runtime.manifest)
                jobs[job_id].update(status="cancelled", error="Learning stopped")
                raise
            except Exception as exc:
                jobs[job_id].update(status="error", error=str(exc) if isinstance(exc, ValueError) else type(exc).__name__)
            finally:
                compile_jobs.pop(connector_id, None)
        task = asyncio.create_task(worker())
        tasks.add(task)
        job_tasks[job_id] = task
        task.add_done_callback(tasks.discard)
        task.add_done_callback(lambda _: job_tasks.pop(job_id, None))
        return {"job_id": job_id}

    def start_repair(connector_id, resource) -> str:
        return start_compilation(connector_id, resource=resource)["job_id"]

    async def run_daily_check():
        return await daily_check(store.list(), runtime_for, busy=lambda cid: cid in compile_jobs, write=store.write,
                                 has_session=lambda cid: (store.home / "auth" / (cid + ".session")).exists(),
                                 audit=audit)

    async def daily_loop(clock):
        while True:
            await asyncio.sleep(seconds_until(clock))
            try:
                await run_daily_check()
            except Exception:
                pass

    app.state.run_daily_check = run_daily_check

    @app.get("/api/errors/{ref}")
    async def error_detail(ref: str):
        row = errors.find(ref.upper())
        if row is None:
            raise HTTPException(404, "No error with that reference")
        return row

    @app.post("/api/check")
    async def check_now():
        return {"sites": await run_daily_check()}

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/playground")
    async def playground():
        return FileResponse(STATIC / "playground.html")

    @app.get("/api/health")
    async def health():
        settings = model_settings()
        return {
            "status": "ok",
            "version": "0.2.0",
            "desktop": os.name == "nt",
            "model_configured": settings["available"],
            "model": settings["model"],
            "model_backend": settings["backend"],
            "home": str(store.home),
        }

    @app.get("/api/apps")
    async def installed_apps():
        from .desktop import bridge

        try:
            return await bridge("catalog")
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/connectors")
    async def connectors():
        return [
            m.model_dump(exclude={"tools", "desktop", "notes"}) | {"tool_count": len(m.tools)}
            for m in store.list()
        ]

    @app.get("/api/connectors/{connector_id}")
    async def connector(connector_id: str, request: Request):
        manifest = manifest_or_404(connector_id)
        return {
            **manifest.model_dump(exclude={"notes"}),
            "client_config": store.config(connector_id),
            "codex_config": store.codex_config(connector_id),
            "universal_config": store.universal_config(str(request.base_url).rstrip("/")),
            "directory": str(store.directory(connector_id)),
            "has_login": (store.home / "auth" / (connector_id + ".session")).exists(),
            "compile_job_id": compile_jobs.get(connector_id),
        }

    @app.get("/api/mcp-config")
    async def universal_config(request: Request):
        return store.universal_config(str(request.base_url).rstrip("/"))

    @app.post("/api/connectors/{connector_id}/compile")
    async def compile_resource(connector_id: str):
        manifest_or_404(connector_id)
        return start_compilation(connector_id)

    def start_build(request: BuildRequest, brief=None) -> str:
        if len(tasks) >= 6:
            raise HTTPException(429, "Several connectors are being built. Wait for one to finish.")
        job_id = uuid4().hex
        prune_jobs()
        jobs[job_id] = {"id": job_id, "status": "queued", "messages": ["Preparing your connector"]}

        async def worker():
            async with build_slots:
                jobs[job_id]["status"] = "running"

                async def progress(message):
                    jobs[job_id]["messages"].append(message)

                try:
                    manifest = await discover(request, store.home, progress)
                    await progress("Packaging the MCP server and connection settings")
                    store.write(manifest)
                    jobs[job_id].update(status="complete", connector_id=manifest.id)
                    if request.kind == "website" and request.compile:
                        runtimes[manifest.id] = Runtime(manifest, store.home)
                        jobs[job_id]["compile_job_id"] = start_compilation(manifest.id, brief=brief)["job_id"]
                except Exception as exc:
                    detail = (
                        str(exc)
                        if isinstance(exc, (ValueError, RuntimeError))
                        else type(exc).__name__ + ": discovery failed"
                    )
                    jobs[job_id].update(status="error", error=detail[:1200])

        task = asyncio.create_task(worker())
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return job_id

    def start_site_build(site: str, name: str | None, brief: str) -> str:
        return start_build(BuildRequest(target=site, name=name or "", compile=True), brief=brief)

    @app.post("/api/build")
    async def build(request: BuildRequest):
        return {"job_id": start_build(request)}

    @app.get("/api/jobs/{job_id}")
    async def job(job_id: str):
        if job_id not in jobs:
            raise HTTPException(404, "Job not found")
        return jobs[job_id]

    @app.post("/api/connectors/{connector_id}/call")
    async def call(connector_id: str, request: CallRequest):
        try:
            return {"result": await runtime_for(connector_id).call(request.tool, request.arguments)}
        except ProgramError as exc:
            store.write(runtime_for(connector_id).manifest)
            return JSONResponse({**error_payload(exc), "error": str(exc)}, status_code=400)
        except HTTPException:
            raise
        except SessionBusy as exc:
            return JSONResponse({**error_payload(exc), "error": str(exc)}, status_code=400)
        except Exception as exc:
            return JSONResponse({"error": safe_message(exc)}, status_code=400)

    @app.post("/api/connectors/{connector_id}/run")
    async def run_task_job(connector_id: str, request: CallRequest):
        runtime = runtime_for(connector_id)
        tool = next(
            (t for t in runtime.manifest.tools if t.name == request.tool and t.adapter == "agent"), None
        )
        if tool is None:
            raise HTTPException(400, "Choose a model task tool from this connector.")
        if len(tasks) >= 6:
            raise HTTPException(429, "Several tasks are running. Wait for one to finish.")
        prune_jobs()
        job_id = uuid4().hex
        jobs[job_id] = {"id": job_id, "status": "running", "messages": ["Starting your task"]}

        async def worker():
            async def progress(message):
                jobs[job_id]["messages"].append(message)

            try:
                result = await runtime.call(request.tool, request.arguments, progress=progress)
                jobs[job_id].update(status="complete", result=result)
            except asyncio.CancelledError:
                jobs[job_id].update(
                    status="cancelled", error="Stopped. Any changes already made remain in the app."
                )
                raise
            except Exception as exc:
                detail = (
                    str(exc)
                    if isinstance(exc, (ValueError, RuntimeError))
                    else type(exc).__name__ + ": task failed"
                )
                jobs[job_id].update(status="error", error=detail[:1200])

        task = asyncio.create_task(worker())
        job_tasks[job_id] = task
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        task.add_done_callback(lambda _: job_tasks.pop(job_id, None))
        return {"job_id": job_id}

    def request_cancel(job_id: str) -> bool:
        task = job_tasks.get(job_id)
        if task:
            # A task cancelled before its first await never enters the worker's handler.
            jobs[job_id].update(
                status="cancelled", error="Stopped. Any changes already made remain in the app."
            )
            task.cancel()
        return bool(task)

    @app.post("/api/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str):
        if job_id not in jobs:
            raise HTTPException(404, "Job not found")
        stopped = request_cancel(job_id)
        return {"message": "Stop requested" if stopped else "This task has already finished"}

    @app.post("/api/connectors/{connector_id}/login/{action}")
    async def login(connector_id: str, action: str):
        runtime = runtime_for(connector_id)
        if not runtime.browser:
            raise HTTPException(400, "Sign in directly in the installed Windows app.")
        async with runtime.lock:
            try:
                if action == "start":
                    await runtime.close()
                    await runtime.acquire_browser()
                    runtime.browser = BrowserSession(runtime.manifest, store.home, headed=True)
                    runtime.browser.manual_login = True
                    await runtime.browser.start()
                    runtime.login_fingerprint = await fingerprint(runtime.browser.page)
                    return {
                        "message": "A separate browser is open. Sign in there, then click Finish login here."
                    }
                if action == "finish":
                    if not runtime.browser.manual_login or not runtime.browser.context:
                        raise HTTPException(400, "Start a login session first.")
                    recovered = await runtime.browser.finish_login() == "checkpoint_recovered"
                    await runtime.browser.close()
                    runtime.browser = BrowserSession(runtime.manifest, store.home)
                    await refresh_forms(runtime.manifest, runtime.browser)
                    check = await confirm(runtime.browser, getattr(runtime, "login_fingerprint", None))
                    runtime.manifest.findings.append({"type": "login_check", **check})
                    store.write(runtime.manifest)
                    if not check["verified"]:
                        return {
                            "message": "Session saved, but the sign-in could not be verified: " + check["reason"]
                            + ". Open login again, or use Learn tools if you are sure you are signed in.",
                            "login": check,
                            "recovered": recovered,
                        }
                    return {
                        "message": "Sign-in verified. Conduit is now learning and verifying CRUD tools.",
                        "login": check,
                        "recovered": recovered,
                        **start_compilation(connector_id),
                    }
                raise HTTPException(404, "Unknown login action")
            except HTTPException:
                raise
            except Exception as exc:
                await runtime.close()
                raise HTTPException(400, "Could not finish this login step: " + type(exc).__name__) from exc

    @app.post("/api/connectors/{connector_id}/refresh")
    async def refresh(connector_id: str):
        runtime = runtime_for(connector_id)
        if not runtime.browser:
            raise HTTPException(400, "Use desktop_snapshot to inspect the app's current controls.")
        async with runtime.lock:
            await runtime.acquire_browser()
            await refresh_forms(runtime.manifest, runtime.browser)
            store.write(runtime.manifest)
        return {"message": "Updated the observed form tools. Restart connected MCP clients to reload them."}

    @app.post("/api/connectors/{connector_id}/release")
    async def release(connector_id: str):
        manifest_or_404(connector_id)
        runtime = runtimes.get(connector_id)
        if runtime:
            async with runtime.lock:
                await runtime.close()
        return {"message": "Preview released. Your MCP client can now use this connector."}

    @app.get("/api/connectors/{connector_id}/export")
    async def export(connector_id: str):
        manifest_or_404(connector_id)
        directory = store.directory(connector_id)
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in directory.rglob("*"):
                if (
                    path.is_file()
                    and path.suffix in (".py", ".ps1", ".json", ".toml", ".txt", ".md")
                    and not any(p in (".state", "__pycache__") for p in path.relative_to(directory).parts)
                ):
                    archive.write(path, path.relative_to(directory))
        return Response(
            output.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{connector_id}.zip"'},
        )

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    gateway = build_gateway(registry=registry, runtime_for=runtime_for, compile_jobs=compile_jobs, jobs=jobs,
                            start_build=start_site_build, start_compilation=start_compilation,
                            cancel_job=request_cancel, persist=store.write, audit=audit, errors=errors,
                            start_repair=start_repair,
                            users=users, home=store.home,
                            model_available=lambda: bool(model_settings().get("available")))
    manager = mount_gateway(app, gateway, json_response=mcp_json_response, users=users)
    app.state.gateway = gateway
    return app
