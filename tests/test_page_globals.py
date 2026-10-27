import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import pytest

from conduit.evidence import EvidenceCollector
from conduit.models import Manifest, schema
from conduit.programs import Family, Program, ProgramError
from conduit.runtime import Runtime
from conduit.verifier import FamilyExecutor, verify_family


@contextmanager
def antiforgery_site():
    """An ASP.NET-style app: the anti-forgery token and the user id only exist in an inline page script."""
    state = {"items": {}, "writes": 0, "token": "af-" + uuid4().hex, "user": "user-" + uuid4().hex[:8]}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, body, status=200, content_type="application/json"):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Set-Cookie", "session=fixture; HttpOnly; Path=/; SameSite=Strict")
            self.end_headers()
            self.wfile.write((body if isinstance(body, str) else json.dumps(body)).encode())

        def do_GET(self):
            if self.path.startswith("/api/"):
                key = self.path.rsplit("/", 1)[-1]
                item = state["items"].get(key)
                return self.send(item or {"error": "missing"}, 200 if item else 404)
            self.send("<!doctype html><title>Forms fixture</title><script>window.AppServerInfo={"
                      f"antiForgeryToken:'{state['token']}',userId:'{state['user']}',build:'1.0'}}</script>"
                      "<h1>Fixture</h1>", content_type="text/html")

        def mutate(self, method):
            if self.headers.get("X-Anti-Forgery") != state["token"] or "session=fixture" not in self.headers.get("Cookie", ""):
                return self.send({"error": "AntiForgery token validation error"}, 403)
            parts = self.path.strip("/").split("/")
            if parts[:3] != ["api", "users", state["user"]]:
                return self.send({"error": "Not a valid user"}, 400)
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length)) if length else {}
            key = parts[4] if len(parts) > 4 else None
            if method == "POST":
                key = uuid4().hex
                state["items"][key] = {"id": key, "title": data["title"]}
            elif method == "PATCH":
                if key not in state["items"]:
                    return self.send({"error": "missing"}, 404)
                state["items"][key]["title"] = data["title"]
            elif method == "DELETE":
                state["items"].pop(key, None)
            state["writes"] += 1
            self.send(state["items"].get(key) or {"deleted": True})

        def do_POST(self):
            self.mutate("POST")

        def do_PATCH(self):
            self.mutate("PATCH")

        def do_DELETE(self):
            self.mutate("DELETE")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


TOKEN = {"$session": {"kind": "global", "key": "AppServerInfo.antiForgeryToken"}}
USER = {"$global": {"key": "window.AppServerInfo.userId"}}


def steps_family():
    properties = {"id": {"type": "string"}, "title": {"type": "string"}}
    base = {"$concat": ["/api/users/", USER, "/forms"]}
    by_id = {"$concat": ["/api/users/", USER, "/forms/", {"$ref": "input.id"}]}
    operations = {
        "create": {"method": "POST", "url": base, "body": {"title": {"$ref": "input.title"}}},
        "read": {"method": "GET", "url": {"$concat": ["/api/forms/", {"$ref": "input.id"}]}, "effect": "read"},
        "update": {"method": "PATCH", "url": by_id, "body": {"title": {"$ref": "input.title"}}},
        "delete": {"method": "DELETE", "url": by_id},
    }
    family = {}
    for verb, request in operations.items():
        step = {"kind": "request", "name": "response", "transport": "http",
                "headers": {"X-Anti-Forgery": TOKEN} if verb != "read" else {}, **request}
        if verb == "read":
            step.update(accept=[200, 404])
        required = ["title"] if verb == "create" else ["id", "title"] if verb == "update" else ["id"]
        steps, result = [step], {"$ref": "vars.response.body"}
        if verb == "read":
            steps.append({"kind": "if", "name": "_absent", "value": {"$equal": [{"$ref": "vars.response.status"}, 404]},
                          "steps": [{"kind": "set", "name": "found", "value": None}],
                          "otherwise": [{"kind": "set", "name": "found", "value": {"$ref": "vars.response.body"}}]})
            result = {"$ref": "vars.found"}
        family[verb] = {"input_schema": schema(properties, required), "program": {"steps": steps, "result": result}}
    return Family(name="forms", label="Fixture forms", operations=family, fields={"title": "title"},
                  create_input={"title": "Conduit {{nonce}}"}, update_input={"title": "Conduit edited {{nonce}}"},
                  evidence=["s1"])


def javascript_family():
    family = steps_family()
    for verb, operation in family.operations.items():
        method = operation.program.steps[0].method
        code = (
            "const user = await api.global('AppServerInfo.userId');"
            "const headers = " + ("{'X-Anti-Forgery': api.session(" + json.dumps(TOKEN["$session"]) + ")}" if verb != "read" else "{}") + ";"
        )
        if verb == "create":
            code += "return (await api.request({url:'/api/users/'+user+'/forms',method:'POST',headers,body:{title:input.title}})).body;"
        elif verb == "read":
            code += ("const r = await api.request({url:'/api/forms/'+input.id,method:'GET',accept:[200,404],effect:'read',"
                     "read_evidence:['s1']}); return r.status === 404 ? null : r.body;")
        elif verb == "update":
            code += "return (await api.request({url:'/api/users/'+user+'/forms/'+input.id,method:'PATCH',headers,body:{title:input.title}})).body;"
        else:
            code += "return (await api.request({url:'/api/users/'+user+'/forms/'+input.id,method:'DELETE',headers})).body;"
        operation.program = Program(javascript=code, read_evidence=["s1"] if verb == "read" else [])
        assert method
    return family


@pytest.mark.integration
@pytest.mark.parametrize("javascript", [False, True])
async def test_inline_script_antiforgery_token_binds_at_call_time(tmp_path, javascript):
    with antiforgery_site() as (url, state):
        manifest = Manifest(id="antiforgery", name="Anti-forgery", kind="website", target=url,
                            allowed_origins=[url], allow_private=True)
        runtime = Runtime(manifest, tmp_path)
        try:
            await runtime.acquire_browser()
            await runtime.browser.start()
            family = javascript_family() if javascript else steps_family()
            report = await verify_family(runtime.browser, manifest, family)
            assert report["status"] == "ready", report
            assert report["restart_tested"] and state["items"] == {}
            collector = EvidenceCollector(runtime.browser, manifest)
            evidence = await collector.collect()
            assert {"kind": "global", "key": "AppServerInfo.antiForgeryToken"} in evidence["session_bindings"]
            assert state["token"] not in json.dumps(evidence)
            family.notes = state["token"]
            with pytest.raises(ValueError, match="runtime session binding"):
                collector.ensure_portable(family)
            executor = FamilyExecutor(runtime.browser, manifest, family)
            await runtime.browser.page.evaluate("delete window.AppServerInfo")
            with pytest.raises(ProgramError) as error:
                await executor.call("create", {"title": "No token"})
            assert error.value.code in ("authentication_required", "adapter_call_failed")
            assert state["writes"] == 15
        finally:
            await runtime.close()


def test_secret_looking_globals_must_use_session_bindings():
    from conduit.programs import ProgramRunner

    class FakePage:
        async def evaluate(self, *_):
            return "value"

    class FakeSession:
        page = FakePage()

    runner = ProgramRunner.__new__(ProgramRunner)
    runner.session = FakeSession()
    import asyncio
    with pytest.raises(ValueError, match="session global binding"):
        asyncio.run(runner.resolve({"$global": {"key": "App.csrfToken"}}))
