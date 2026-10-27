import asyncio
import html
import re

from fastapi.testclient import TestClient
from mcp.shared.memory import create_connected_server_and_client_session
from test_gateway import READ, receipt

from conduit.programs import ProgramError


class FakeBrowser:
    """Stands in for the headed sign-in window and the headless check that follows it."""

    def __init__(self, manifest, home, *, headed=False):
        self.manifest, self.headed = manifest, headed
        self.context = self.page = None
        self.manual_login = False

    def session_changed_on_disk(self):
        return False

    async def start(self):
        self.context, self.page = object(), object()

    async def finish_login(self):
        return "saved"

    async def save(self):
        return True

    async def close(self):
        self.context = None


def signed_out_until(signed_in, calls):
    async def call(self, name, arguments, progress=None):
        calls.append(name)
        if not signed_in:
            exc = ProgramError("javascript_failed", "missing user info")
            exc.sign_in, exc.sign_in_reason = "strong", "the site sent the browser elsewhere"
            raise exc
        return receipt(name, arguments)
    return call


async def test_a_sign_out_gives_a_link_that_signs_in_and_the_call_then_works(app, monkeypatch):
    calls, watched, signed_in = [], [], []

    async def watch(session, before, seconds, **kwargs):
        watched.append((session.headed, session.manual_login, seconds))
        signed_in.append(True)
        return True

    async def confirm(session, before, **kwargs):
        return {"verified": True, "reason": "a sign-out control is visible"}

    async def fingerprint(page):
        return {"url": "https://example.com/login"}

    monkeypatch.setattr("conduit.runtime.Runtime.call", signed_out_until(signed_in, calls))
    monkeypatch.setattr("conduit.app.BrowserSession", FakeBrowser)
    monkeypatch.setattr("conduit.runtime.BrowserSession", FakeBrowser)
    monkeypatch.setattr("conduit.app.watch_for_sign_in", watch)
    monkeypatch.setattr("conduit.app.confirm", confirm)
    monkeypatch.setattr("conduit.app.fingerprint", fingerprint)
    with TestClient(app) as web:
        async with create_connected_server_and_client_session(app.state.gateway) as session:
            first = (await session.call_tool(READ, {"id": "r1"})).content[0].text
            path = re.search(r"http://127\.0\.0\.1:4455(/signin/[A-Za-z0-9_-]{22})$", first).group(1)
            again = (await session.call_tool(READ, {"id": "r1"})).content[0].text
            assert again == first
            page = web.get(path)
            assert page.status_code == 200 and "Sign in to example.com" in page.text and watched == []
            assert "<form method=\"post\"" in page.text
            started = web.post(path + "/start", follow_redirects=False)
            assert started.status_code == 303 and started.headers["location"] == path + "/status"
            for _ in range(40):
                status = html.unescape(web.get(path + "/status").text)
                if "You're signed in" in status:
                    break
                await asyncio.sleep(0.05)
            assert "You're signed in" in status
            retry = await session.call_tool(READ, {"id": "r1"})
            reused = web.post(path + "/start")
    assert watched == [(True, True, 600)] and not retry.isError
    assert "You're signed in" in html.unescape(reused.text)
    assert app.state.store.get("demo-1").findings[-1]["type"] == "login_check"


def test_link_pages_never_act_on_a_get_and_expire(app):
    links = app.state.links
    token = links.sign_in("demo-1")
    with TestClient(app) as web:
        assert web.get(f"/signin/{token}").status_code == 200
        assert links.get(token, "sign_in")["state"] == "new"
        assert web.get("/signin/not-a-token").status_code == 404
        assert web.post("/approve/not-a-token/yes").status_code == 404
        links.get(token, "sign_in")["expires"] = 0
        assert web.get(f"/signin/{token}").status_code == 404


async def test_a_build_waiting_for_a_sign_in_resumes_with_its_request(app, monkeypatch):
    briefs = []

    async def compile_after(runtime, *, progress=None, brief=None, **kwargs):
        briefs.append(brief)
        return {"status": "ready"} if len(briefs) > 1 else {"status": "login_required"}

    async def watch(session, before, seconds, **kwargs):
        return True

    async def confirm(session, before, **kwargs):
        return {"verified": True, "reason": "ok"}

    async def fingerprint(page):
        return {}

    monkeypatch.setattr("conduit.app.compile_connector", compile_after)
    monkeypatch.setattr("conduit.app.BrowserSession", FakeBrowser)
    monkeypatch.setattr("conduit.runtime.BrowserSession", FakeBrowser)
    monkeypatch.setattr("conduit.app.watch_for_sign_in", watch)
    monkeypatch.setattr("conduit.app.confirm", confirm)
    monkeypatch.setattr("conduit.app.fingerprint", fingerprint)
    monkeypatch.setattr("conduit.app.model_settings", lambda: {"available": True})
    (app.state.store.home / "auth").mkdir(exist_ok=True)
    (app.state.store.home / "auth" / "demo-1.session").write_bytes(b"JSON\n{}")
    with TestClient(app) as web:
        async with create_connected_server_and_client_session(app.state.gateway) as session:
            queued =(await session.call_tool("seira_build", {"site": "https://example.com",
                                                               "request": "archive a record"})).structuredContent
            for _ in range(40):
                status = (await session.call_tool("seira_status", {"job": queued["job"]})).structuredContent
                if status["status"] != "building" and status["status"] != "waiting":
                    break
                await asyncio.sleep(0.05)
            assert status["status"] == "sign_in_required", status
            path = re.search(r"(/signin/[A-Za-z0-9_-]{22})$", status["message"]).group(1)
            web.post(path + "/start")
            for _ in range(60):
                status = (await session.call_tool("seira_status", {"job": queued["job"]})).structuredContent
                if status["status"] in ("ready", "failed"):
                    break
                await asyncio.sleep(0.05)
    assert briefs == ["archive a record", "archive a record"]
    assert status["status"] == "ready", status
