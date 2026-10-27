import asyncio
import json
import os
import re
import time

from fastapi.testclient import TestClient
from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session
from test_compiler import family_for
from test_registry import READY

from conduit.browser import browser_tools
from conduit.compiler import publish
from conduit.models import Manifest
from conduit.programs import ProgramError
from conduit.registry import public_name

REFERENCE = re.compile(r"Reference ([A-Z2-7]{8})\.")

READ = public_name("demo-1", "records_read")
DELETE = public_name("demo-1", "records_delete")

SIGN = r"http://127\.0\.0\.1:4455/signin/[A-Za-z0-9_-]{22}"


def receipt(name, arguments):
    verb = name.rsplit("_", 1)[-1]
    return {"status": "verified", "operation": verb, "id": arguments.get("id", "r1"),
            "data": None if verb == "delete" else {"title": "Kept"}, "deleted": verb == "delete",
            "reconciled": True, "adapter_version": "1",
            "state_token": None,
            "metrics": {"requests": 1, "steps": 1, "model_calls": 0, "screenshots": 0, "duration_ms": 1.5}}


async def test_lists_only_verified_tools_with_honest_hints(app):
    async with create_connected_server_and_client_session(app.state.gateway) as session:
        listed = {t.name: t for t in (await session.list_tools()).tools}
    expected = {public_name("demo-1", f"records_{v}") for v in ("create", "read", "update", "delete")}
    assert set(listed) == expected | {"seira_find", "seira_build", "seira_status", "seira_block", "seira_unblock"}
    read, delete = listed[READ], listed[public_name("demo-1", "records_delete")]
    assert read.annotations.readOnlyHint and not read.annotations.destructiveHint
    assert delete.annotations.destructiveHint and not delete.annotations.readOnlyHint
    assert read.description == "example.com: read a record by its ID."
    assert delete.description == "example.com: delete a record. The user approves each delete."
    assert "metrics" not in read.outputSchema["properties"] and "metrics" not in read.outputSchema["required"]


async def test_instructions_and_resources_never_contain_notes(app):
    assert "SECRET-NOTE" not in app.state.gateway.instructions
    async with create_connected_server_and_client_session(app.state.gateway) as session:
        text = (await session.read_resource("conduit://capabilities")).contents[0].text
    assert "SECRET-NOTE" not in text
    assert json.loads(text) == [{"site": "example.com", "what": "records",
                                 "actions": ["create", "read", "update", "delete"], "status": "ready"}]


async def test_call_returns_the_runtime_receipt(app, monkeypatch):
    seen = {}

    async def fake_call(self, name, arguments, progress=None):
        seen.update(connector=self.manifest.id, name=name)
        return receipt(name, arguments)

    monkeypatch.setattr("conduit.runtime.Runtime.call", fake_call)
    async with create_connected_server_and_client_session(app.state.gateway) as session:
        result = await session.call_tool(READ, {"id": "r1"})
    assert not result.isError and seen == {"connector": "demo-1", "name": "records_read"}
    assert result.structuredContent["id"] == "r1" and result.structuredContent["status"] == "verified"
    assert not {"metrics", "adapter_version", "reconciled"} & set(result.structuredContent)


async def test_an_uncertain_write_says_dont_repeat_and_keeps_the_detail_local(app, monkeypatch):
    exc = ProgramError("postcondition_failed", "The record did not settle at https://example.com/x", uncertain=True)
    exc.object_id = "r1"

    async def fail(self, name, arguments, progress=None):
        raise exc

    monkeypatch.setattr("conduit.runtime.Runtime.call", fail)
    async with create_connected_server_and_client_session(app.state.gateway) as session:
        result = await session.call_tool(public_name("demo-1", "records_create"), {"title": "x"})
    text = result.content[0].text
    assert result.isError and text.startswith(
        "Seira couldn't confirm whether the record was created (ID r1). Don't repeat it; read it first to check.")
    ref = REFERENCE.search(text).group(1)
    assert "settle" not in text and "postcondition" not in text
    detail = app.state.errors.find(ref)
    assert detail["code"] == "postcondition_failed" and "<url>" in detail["message"]
    assert app.state.audit.tail()[-1]["ref"] == ref


async def test_unknown_and_busy_tools_are_refused(app, monkeypatch):
    async def never(self, name, arguments, progress=None):
        raise AssertionError("a refused call must not reach the runtime")

    monkeypatch.setattr("conduit.runtime.Runtime.call", never)
    async with create_connected_server_and_client_session(app.state.gateway) as session:
        unknown = await session.call_tool("deadbeef__records_read", {})
        assert unknown.isError and unknown.content[0].text == "That tool isn't available. Use seira_find to see what is."
        app.state.compile_jobs["demo-1"] = "job-1"
        busy = await session.call_tool(READ, {"id": "r1"})
    assert busy.isError and busy.content[0].text == "Seira is busy with example.com right now. Try again in a minute."


async def test_meta_tools_find_and_status(app):
    async with create_connected_server_and_client_session(app.state.gateway) as session:
        found = (await session.call_tool("seira_find", {"request": "get a record on example.com"})).structuredContent
        none = (await session.call_tool("seira_find", {"request": "book a flight"})).structuredContent
        status = (await session.call_tool("seira_status", {"job": "nope"})).structuredContent
    assert found == {"status": "found", "tool": READ, "site": "example.com", "action": "read", "what": "records"}
    assert none["status"] == "none" and none["sites"] == ["example.com"] and "seira_build" in none["message"]
    assert status == {"status": "unknown", "job": "nope", "message": "Seira has no build with that number."}


async def test_seira_build_returns_at_once_and_the_job_completes(app, monkeypatch):
    calls = {}

    async def fake_discover(request, home, progress):
        await progress("Discovering")
        return Manifest(id="newsite-1", name="New Site", kind="website", target=request.target,
                        allowed_origins=[request.target], tools=browser_tools())

    async def fake_compile(runtime, *, progress=None, brief=None, **kwargs):
        calls["brief"] = brief
        await asyncio.sleep(0.05)
        family = family_for(runtime.manifest.target + "/tasks").model_copy(update={"name": "tasks", "label": "tasks"})
        publish(runtime.manifest, family, {**READY, "resource": "tasks"})
        return {"status": "ready"}

    monkeypatch.setattr("conduit.app.discover", fake_discover)
    monkeypatch.setattr("conduit.app.compile_connector", fake_compile)
    monkeypatch.setattr("conduit.app.model_settings", lambda: {"available": True})
    async with create_connected_server_and_client_session(app.state.gateway) as session:
        started = time.perf_counter()
        queued = (await session.call_tool("seira_build", {"site": "https://newsite.example",
                                                           "request": "add a task on newsite"})).structuredContent
        assert time.perf_counter() - started < 1.0
        assert queued["status"] == "queued" and "2 to 20 minutes" in queued["message"]
        assert set(queued) == {"status", "job", "message"}
        for _ in range(40):
            report = (await session.call_tool("seira_status", {"job": queued["job"]})).structuredContent
            if report["status"] in ("ready", "failed"):
                break
            await asyncio.sleep(0.05)
        listed = {t.name for t in (await session.list_tools()).tools}
    assert report["status"] == "ready", report
    assert public_name("newsite-1", "tasks_create") in report["tools"] and "connector_id" not in report
    assert public_name("newsite-1", "tasks_delete") in listed
    assert calls["brief"] == "add a task on newsite"
    assert app.state.store.get("newsite-1").notes == ""


async def test_seira_build_refuses_duplicates_and_asks_for_login(app, monkeypatch):
    monkeypatch.setattr("conduit.app.model_settings", lambda: {"available": True})
    store = app.state.store
    store.write(Manifest(id="nosess-1", name="No Session", kind="website", target="https://nosession.example",
                         allowed_origins=["https://nosession.example"], tools=browser_tools()))
    async with create_connected_server_and_client_session(app.state.gateway) as session:
        exists = (await session.call_tool("seira_build", {"site": "https://example.com",
                                                           "request": "get a record"})).structuredContent
        login = (await session.call_tool("seira_build", {"site": "https://nosession.example",
                                                          "request": "add a widget"})).structuredContent
    assert exists["status"] == "exists" and exists["tool"] == READ
    assert login["status"] == "sign_in_required"
    assert re.fullmatch(r"You're signed out of nosession\.example\. Sign in here, then ask again: http://127\.0\.0\.1:4455/signin/[A-Za-z0-9_-]{22}",
                        login["message"])
    assert not app.state.jobs


async def test_runtime_reloads_when_connector_json_changes(app, monkeypatch):
    async def fake_call(self, name, arguments, progress=None):
        return receipt(name, arguments) | {"data": {"tools": sorted(t.name for t in self.manifest.tools)}}

    monkeypatch.setattr("conduit.runtime.Runtime.call", fake_call)
    store = app.state.store
    async with create_connected_server_and_client_session(app.state.gateway) as session:
        first = (await session.call_tool(READ, {"id": "r1"})).structuredContent["data"]["tools"]
        before = app.state.runtimes["demo-1"]
        manifest = store.get("demo-1")
        manifest.tools = [t for t in manifest.tools if t.name != "records_delete"]
        path = store.write(manifest) / "connector.json"
        os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 1_000_000_000))
        second = (await session.call_tool(READ, {"id": "r1"})).structuredContent["data"]["tools"]
    assert "records_delete" in first and "records_delete" not in second
    assert app.state.runtimes["demo-1"] is not before


def answering(confirm):
    async def callback(context, params):
        assert params.mode == "form" and params.message in (
            "Delete the record (ID r1) on example.com? This can't be undone.",
            "Delete the record (ID r2) on example.com? This can't be undone.")
        return types.ElicitResult(action="accept" if confirm is not None else "decline",
                                  content={"confirm": confirm} if confirm is not None else None)
    return callback


async def test_delete_asks_the_user_through_elicitation_and_logs_the_answer(app, monkeypatch):
    calls = []

    async def fake_call(self, name, arguments, progress=None):
        calls.append((name, arguments))
        return receipt(name, arguments)

    monkeypatch.setattr("conduit.runtime.Runtime.call", fake_call)
    async with create_connected_server_and_client_session(app.state.gateway,
                                                           elicitation_callback=answering(True)) as session:
        done = await session.call_tool(DELETE, {"id": "r1"})
    async with create_connected_server_and_client_session(app.state.gateway,
                                                           elicitation_callback=answering(None)) as session:
        declined = await session.call_tool(DELETE, {"id": "r2"})
    assert not done.isError and calls == [("records_delete", {"id": "r1"})]
    assert declined.isError and declined.content[0].text == "The delete wasn't approved, so nothing was changed."
    rows = app.state.audit.tail()
    assert [(r["verb"], r["approval"], r["outcome"]) for r in rows] == [
        ("delete", "elicitation", "ok"), ("delete", "elicitation", "not_confirmed")]
    assert "r1" not in json.dumps(rows) and rows[0]["user"] == "local"


async def test_delete_without_elicitation_runs_only_after_the_person_approves_the_link(app, monkeypatch):
    calls = []

    async def fake_call(self, name, arguments, progress=None):
        calls.append(arguments)
        return receipt(name, arguments)

    monkeypatch.setattr("conduit.runtime.Runtime.call", fake_call)
    with TestClient(app) as web:
        async with create_connected_server_and_client_session(app.state.gateway) as session:
            listed = {t.name: t for t in (await session.list_tools()).tools}
            first = (await session.call_tool(DELETE, {"id": "r1"})).content[0].text
            match = re.fullmatch(r"Approve deleting the record \(ID r1\) on example\.com here: "
                                 r"http://127\.0\.0\.1:4455(/approve/[A-Za-z0-9_-]{22}) Once it's approved, ask me again\.",
                                 first)
            path = match.group(1)
            page = web.get(path)
            assert page.status_code == 200 and "Delete this record?" in page.text and "(ID r1)" in page.text
            still_waiting = (await session.call_tool(DELETE, {"id": "r1"})).content[0].text
            assert still_waiting == first and calls == []
            assert web.post(path + "/yes", follow_redirects=False).status_code == 303
            other = (await session.call_tool(DELETE, {"id": "r9"})).content[0].text
            done = await session.call_tool(DELETE, {"id": "r1"})
            again = (await session.call_tool(DELETE, {"id": "r1"})).content[0].text
            assert web.post(path + "/yes").status_code == 200
    assert "confirm_token" not in listed[DELETE].inputSchema.get("properties", {})
    assert other.startswith("Approve deleting the record (ID r9)")
    assert not done.isError and calls == [{"id": "r1"}]
    assert again.startswith("Approve deleting the record (ID r1)") and calls == [{"id": "r1"}]
    assert app.state.audit.tail()[-1]["approval"] == "approval_link"


async def test_seira_build_needs_a_model(app, monkeypatch):
    monkeypatch.setattr("conduit.app.model_settings", lambda: {"available": False})
    async with create_connected_server_and_client_session(app.state.gateway) as session:
        result = await session.call_tool("seira_build", {"site": "https://other.example", "request": "add a thing"})
    assert result.isError and result.content[0].text == (
        "Seira can't build new tools right now. Your administrator can see why.")
