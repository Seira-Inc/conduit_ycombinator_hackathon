import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs
from uuid import uuid4

import pytest

from conduit.evidence import EvidenceCollector
from conduit.models import Manifest, schema
from conduit.programs import Family, Program, ProgramError, validate_program
from conduit.runtime import Runtime
from conduit.verifier import FamilyExecutor, verify_family


@contextmanager
def rpc_site():
    state = {"items": {}, "writes": 0, "denied": False, "token": "test-session-value-" + uuid4().hex}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, body, content_type="application/json"):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Set-Cookie", "session=fixture; HttpOnly; Path=/; SameSite=Strict")
            self.end_headers()
            self.wfile.write((body if isinstance(body, str) else json.dumps(body)).encode())

        def do_GET(self):
            self.send("<h1>Session RPC fixture</h1>", "text/html")

        def do_POST(self):
            assert self.headers["Content-Type"].startswith("application/x-www-form-urlencoded")
            data = {k: v[0] for k, v in parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode()).items()}
            if data.get("token") != state["token"] or "session=fixture" not in self.headers.get("Cookie", ""):
                return self.send({"ok": False, "error": "invalid_auth"})
            method = self.path.rsplit(".", 1)[-1]
            key = data.get("id")
            if method == "create":
                key = uuid4().hex
                state["items"][key] = {"id": key, "title": data["title"], "note": data.get("note", "")}
            elif method == "update":
                if state["denied"]:
                    return self.send({"ok": False, "error": "edit_denied"})
                state["items"][key]["title"] = data["title"]
            elif method == "delete":
                state["items"].pop(key, None)
            if method != "read":
                state["writes"] += 1
            self.send({"ok": True, "item": state["items"].get(key)})
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def rpc_family(transport):
    properties = {"id": {"type": "string"}, "title": {"type": "string"}, "note": {"type": "string"}}
    session = {"$session": {"kind": "storage", "key": "app_config", "path": [
        "teams", {"$session": {"kind": "storage", "key": "active_team"}}, "token"]}}
    operations = {}
    for verb in ("create", "read", "update", "delete"):
        request = {"kind": "request", "name": "response", "method": "POST", "url": "/rpc/items." + verb,
            "transport": transport, "encoding": "form", "headers": {"Content-Type": "application/x-www-form-urlencoded"},
            "body": {"$merge": [{"$ref": "input"}, {"token": session}]},
            "response_check": {"path": "ok", "equals": True, "error_path": "error",
                               "authentication_errors": ["invalid_auth"]}}
        if verb == "read":
            request.update(effect="read", read_evidence=["s1"])
        required = ["title"] if verb == "create" else ["id", "title"] if verb == "update" else ["id"]
        operations[verb] = {"input_schema": schema(properties, required), "program": {
            "steps": [request], "result": {"$ref": "vars.response.body.item"}}}
    return Family(name="records", label="RPC records", operations=operations, fields={"title": "title", "note": "note"},
        create_input={"title": "Conduit {{nonce}}", "note": "Unicode café & + = /"},
        update_input={"title": "Conduit edited {{nonce}}"}, evidence=["s1"])


@pytest.mark.integration
@pytest.mark.parametrize("transport", ["http", "page"])
@pytest.mark.parametrize("javascript", [False, True])
async def test_form_rpc_crud_uses_current_nested_account_session(tmp_path, transport, javascript):
    with rpc_site() as (url, state):
        manifest = Manifest(id="rpc", name="RPC", kind="website", target=url,
                            allowed_origins=[url], allow_private=True)
        runtime = Runtime(manifest, tmp_path)
        try:
            await runtime.acquire_browser()
            await runtime.browser.start()
            await runtime.browser.page.evaluate("""token => {
                localStorage.setItem('app_config',JSON.stringify({teams:{A:{token:'wrong-first-account'},B:{token}}}));
                localStorage.setItem('active_team','B');
            }""", state["token"])
            family = rpc_family(transport)
            if javascript:
                for operation in family.operations.values():
                    step = operation.program.steps[0]
                    token = step.body["$merge"][1]["token"]["$session"]
                    options = step.model_dump(exclude_defaults=True, exclude={"kind", "name", "body"})
                    operation.program = Program(read_evidence=step.read_evidence, javascript=(
                        'const options=' + json.dumps(options) + ';'
                        'options.body={...input,token:api.session(' + json.dumps(token) + ')};'
                        'return (await api.request(options)).body.item;'))
            report = await verify_family(runtime.browser, manifest, family)
            assert report["status"] == "ready", report
            assert report["restart_tested"]
            assert state["items"] == {} and state["writes"] == 15
            executor = FamilyExecutor(runtime.browser, manifest, family)
            created = await executor.call("create", {"title": "Conduit actual + & café", "note": "Keep"})
            assert state["items"][created["id"]]["title"] == "Conduit actual + & café"
            state["denied"] = True
            with pytest.raises(ProgramError, match="edit_denied") as error:
                await executor.call("update", {"id": created["id"], "title": "Denied"})
            assert error.value.code == "application_error"
            assert state["writes"] == 16
            await executor.call("delete", {"id": created["id"]})
            await runtime.browser.recorder.flush()
            assert state["token"] not in json.dumps(runtime.browser.recorder.events)
            collector = EvidenceCollector(runtime.browser, manifest)
            evidence = await collector.collect()
            assert {"kind": "storage", "key": "app_config", "path": ["teams", "B", "token"]} in evidence["session_bindings"]
            assert state["token"] not in json.dumps(evidence)
            family.notes = state["token"]
            with pytest.raises(ValueError, match="runtime session binding"):
                collector.ensure_portable(family)
            await runtime.browser.page.evaluate("localStorage.setItem('active_team','A')")
            with pytest.raises(ProgramError) as error:
                await executor.call("create", {"title": "Wrong account"})
            assert error.value.code == "authentication_required" and error.value.uncertain is False
            assert state["writes"] == 17
        finally:
            await runtime.close()


def test_post_rpc_is_not_assumed_read_only():
    family = rpc_family("http")
    program = family.operations["read"].program
    program.steps[0].read_evidence = []
    with pytest.raises(ValueError, match="operation evidence"):
        validate_program(program, read_only=True)
