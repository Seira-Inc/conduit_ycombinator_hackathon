import asyncio
import html
import re
import socket
from contextlib import asynccontextmanager

import pytest
import uvicorn
from fastapi.testclient import TestClient
from mcp.shared.memory import create_connected_server_and_client_session
from playwright.async_api import async_playwright
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

    def _session_mtime(self):
        return None

    async def start(self):
        self.context, self.page = object(), object()

    async def finish_login(self):
        return "saved"

    async def checkpoint(self):
        return True

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
    assert len(watched) == 1 and watched[0][:2] == (True, True) and 590 < watched[0][2] <= 600
    assert not retry.isError
    assert "You're signed in" in html.unescape(reused.text)
    assert app.state.store.get("demo-1").findings[-1]["type"] == "login_check"


async def test_the_sign_in_window_stays_open_until_its_cookies_work(app, monkeypatch):
    """A site can look signed in before it has finished; only a separate browser opening it with the
    saved cookies decides, and the window closes after that, not before."""
    events = []

    class Window(FakeBrowser):
        async def checkpoint(self):
            events.append("saved")
            return True

        async def close(self):
            events.append("window closed" if self.headed else "closed")
            await super().close()

    async def watch(session, before, seconds, **kwargs):
        return True

    async def confirm(session, before, **kwargs):
        events.append("tried")
        return {"verified": events.count("tried") >= 2, "reason": "ok"}

    async def fingerprint(page):
        return {}

    monkeypatch.setattr("conduit.app.SIGN_IN_RETRY", 0.01)
    monkeypatch.setattr("conduit.app.BrowserSession", Window)
    monkeypatch.setattr("conduit.runtime.BrowserSession", Window)
    monkeypatch.setattr("conduit.app.watch_for_sign_in", watch)
    monkeypatch.setattr("conduit.app.confirm", confirm)
    monkeypatch.setattr("conduit.app.fingerprint", fingerprint)
    token = app.state.links.sign_in("demo-1")
    with TestClient(app) as web:
        web.post(f"/signin/{token}/start")
        for _ in range(60):
            if "You're signed in" in html.unescape(web.get(f"/signin/{token}/status").text):
                break
            await asyncio.sleep(0.05)
    before_close = events[:events.index("window closed")]
    assert before_close.count("tried") == 2 and before_close.count("saved") == 2
    assert app.state.links.get(token, "sign_in")["progress"] == "signed_in"


def signing_in_with(monkeypatch, *, watch, verified):
    async def confirm(session, before, **kwargs):
        return {"verified": verified, "reason": "ok"}

    async def fingerprint(page):
        return {}

    monkeypatch.setattr("conduit.app.SIGN_IN_RETRY", 0.01)
    monkeypatch.setattr("conduit.app.BrowserSession", FakeBrowser)
    monkeypatch.setattr("conduit.runtime.BrowserSession", FakeBrowser)
    monkeypatch.setattr("conduit.app.watch_for_sign_in", watch)
    monkeypatch.setattr("conduit.app.confirm", confirm)
    monkeypatch.setattr("conduit.app.fingerprint", fingerprint)


async def status_once(web, token, words):
    for _ in range(60):
        text = html.unescape(web.get(f"/signin/{token}/status").text)
        if words in text:
            return text
        await asyncio.sleep(0.05)
    return text


async def test_the_person_says_when_the_sign_in_is_done(app, monkeypatch):
    async def never_sure(session, before, seconds, **kwargs):
        await asyncio.sleep(seconds)
        return False

    signing_in_with(monkeypatch, watch=never_sure, verified=True)
    token = app.state.links.sign_in("demo-1")
    with TestClient(app) as web:
        web.post(f"/signin/{token}/start")
        waiting = await status_once(web, token, "I've signed in")
        assert "Leave the window open" in waiting
        assert web.post(f"/signin/{token}/done", follow_redirects=False).status_code == 303
        signed_in = await status_once(web, token, "You're signed in")
    assert "You're signed in" in signed_in


async def test_a_window_that_vanishes_by_itself_is_named_as_such(app, monkeypatch):
    async def closed(session, before, seconds, **kwargs):
        session.watch_note = {"ended": "window closed", "path": []}
        return False

    signing_in_with(monkeypatch, watch=closed, verified=False)
    token = app.state.links.sign_in("demo-1")
    with TestClient(app) as web:
        web.post(f"/signin/{token}/start")
        text = await status_once(web, token, "The sign-in window closed")
    assert "The sign-in window closed" in text
    assert (app.state.store.home / "signin.jsonl").read_text(encoding="utf-8").count("window closed") >= 1


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


@asynccontextmanager
async def serving(app):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                await asyncio.sleep(0.02)
        yield "http://127.0.0.1:" + str(listener.getsockname()[1])
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 15)
        listener.close()


@pytest.mark.integration
async def test_the_link_buttons_work_in_a_real_browser(app, monkeypatch):
    """A real browser sends the page's origin with a form's POST, which the test client never does."""
    async def watch(session, before, seconds, **kwargs):
        return True

    async def confirm(session, before, **kwargs):
        return {"verified": True, "reason": "ok"}

    async def fingerprint(page):
        return {}

    monkeypatch.setattr("conduit.app.BrowserSession", FakeBrowser)
    monkeypatch.setattr("conduit.runtime.BrowserSession", FakeBrowser)
    monkeypatch.setattr("conduit.app.watch_for_sign_in", watch)
    monkeypatch.setattr("conduit.app.confirm", confirm)
    monkeypatch.setattr("conduit.app.fingerprint", fingerprint)
    sign_in = app.state.links.sign_in("demo-1")
    approve = app.state.links.approval(READ, "digest", {"site": "example.com", "thing": "record", "id_note": ""})
    async with serving(app) as base:
        playwright = await async_playwright().start()
        try:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            await page.goto(f"{base}/signin/{sign_in}")
            await page.get_by_role("button", name="Open the sign-in window").click()
            await page.get_by_role("heading", name="You're signed in").wait_for(timeout=10000)
            await page.goto(f"{base}/approve/{approve}")
            await page.get_by_role("button", name="Delete it").click()
            await page.get_by_text("Approved.").wait_for(timeout=10000)
            await browser.close()
        finally:
            await playwright.stop()
    assert app.state.links.get(approve, "approval")["state"] == "approved"
