import asyncio
import re
import time

import pytest
from fastapi.testclient import TestClient
from mcp.shared.memory import create_connected_server_and_client_session
from test_gateway import DELETE, READ

from conduit.browser import BrowserSession, browser_tools
from conduit.login import confirm, login_finding, recently_signed_in, sign_in_pages, watch_for_sign_in
from conduit.models import Manifest
from conduit.programs import ProgramError
from conduit.runtime import Runtime

SIGN = r"http://127\.0\.0\.1:4455/signin/[A-Za-z0-9_-]{22}"
PLAIN = {"password_inputs": 0, "identity_inputs": 0, "sign_in_controls": 0, "sign_out_controls": 0, "text": ""}
SIGN_IN_BUTTON = {**PLAIN, "sign_in_controls": 1}
SIGN_OUT_BUTTON = {**PLAIN, "sign_out_controls": 1}
PASSWORD = {**PLAIN, "password_inputs": 1, "identity_inputs": 1, "sign_in_controls": 1}


def signed_in_once(before, after):
    return login_finding({"verified": True, "reason": "ok", "before": before, "after": after})


def forms(history=True):
    manifest = Manifest(id="forms", name="Forms", kind="website", target="https://forms.example/",
                        allowed_origins=["https://forms.example"], tools=browser_tools())
    if history:
        manifest.findings.append(signed_in_once({"url": "https://forms.example/", "title": "Forms - free surveys"},
                                                {"url": "https://forms.example/Pages/Design?x=1", "title": "Forms"}))
    return manifest


class ScriptedPage:
    """A page that moves through (seconds after load, path or url, title, facts) states."""

    def __init__(self, states):
        self.states, self.loaded = states, time.monotonic()

    def _now(self):
        elapsed = time.monotonic() - self.loaded
        return [s for s in self.states if s[0] <= elapsed][-1]

    @property
    def url(self):
        where = self._now()[1]
        return where if where.startswith("http") else "https://forms.example" + where

    def is_closed(self):
        return False

    async def goto(self, *args, **kwargs):
        self.loaded = time.monotonic()

    async def evaluate(self, script):
        return self._now()[3]

    async def title(self):
        return self._now()[2]

    async def wait_for_load_state(self, *args, **kwargs):
        return None


@pytest.fixture
def quick(monkeypatch):
    monkeypatch.setattr("conduit.browser.SETTLE", 1.0)
    monkeypatch.setattr("conduit.browser.SETTLE_NO_SIGN_IN", 0.3)
    monkeypatch.setattr("conduit.browser.DOUBT_HOLDS", 0.3)
    monkeypatch.setattr("conduit.browser.POLL", 0.05)


async def judge(tmp_path, manifest, states):
    browser = BrowserSession(manifest, tmp_path)
    browser.context, browser.page, browser.login_return_wait = object(), ScriptedPage(states), 0
    return await browser.check_sign_in()


@pytest.mark.parametrize("history, states, expected", [
    (True, [(0, "/", "Forms - free surveys", PLAIN), (0.2, "/", "Forms - free surveys", SIGN_IN_BUTTON)],
     "signed_out"),
    (True, [(0, "/", "Loading", PLAIN), (0.2, "/Pages/Design", "Forms", PLAIN)], "signed_in"),
    (True, [(0, "/", "Loading", SIGN_IN_BUTTON), (0.1, "/home", "Home", SIGN_OUT_BUTTON)], "signed_in"),
    (True, [(0, "/home", "Home", PLAIN)], "unsure"),
    (True, [(0, "/", "Loading", PLAIN), (0.2, "https://login.other.example/authorize", "Sign in", PLAIN)],
     "signed_out"),
    (False, [(0, "/", "Draw", SIGN_IN_BUTTON)], "signed_in"),
    (False, [(0, "/login", "Sign in", PASSWORD)], "signed_out"),
], ids=["sign-in-page-settles", "loading-becomes-app", "sign-in-flashes-past", "nothing-decisive",
        "sent-to-another-site", "never-asked-for-sign-in", "password-anywhere"])
async def test_the_check_waits_for_the_page_to_settle_before_judging(tmp_path, quick, history, states, expected):
    state, reason = await judge(tmp_path, forms(history), states)
    assert state == expected and reason


def test_only_pages_that_differ_become_markers():
    same = forms(False)
    same.findings.append(signed_in_once({"url": "https://forms.example/", "title": "App"},
                                        {"url": "https://forms.example/", "title": "App"}))
    loading = forms(False)
    loading.findings.append(signed_in_once({"url": "https://forms.example/x", "title": "Loading..."},
                                           {"url": "https://forms.example/x", "title": "Contacts"}))
    assert sign_in_pages(same) == {}
    assert set(sign_in_pages(loading)) == {"in"}
    assert sign_in_pages(forms()) == {"in": ("https://forms.example", "/Pages/Design", "Forms"),
                                      "out": ("https://forms.example", "/", "Forms - free surveys")}


def test_a_recent_sign_in_is_trusted_for_half_an_hour():
    old = forms(False)
    old.findings.append({**signed_in_once({}, {}), "at": "2026-01-01T09:00:00+0000"})
    failed = forms(False)
    failed.findings.append({**signed_in_once({}, {}), "verified": False})
    assert recently_signed_in(forms()) and not recently_signed_in(old) and not recently_signed_in(failed)


@pytest.mark.sign_in_check
async def test_the_runtime_looks_once_then_again_when_old_or_the_session_changes(tmp_path, monkeypatch):
    looks, mtime = [], [1]

    class Browser:
        def __init__(self, manifest, home, *, headed=False):
            self.context, self.manual_login = None, False

        def session_changed_on_disk(self):
            return False

        def _session_mtime(self):
            return mtime[0]

        async def check_sign_in(self):
            looks.append(1)
            return "signed_out", "a sign-in control is visible and no sign-out control"

        async def close(self):
            self.context = None

    monkeypatch.setattr("conduit.runtime.BrowserSession", Browser)
    runtime = Runtime(forms(), tmp_path)
    try:
        assert (await runtime.check_sign_in())[0] == "signed_out"
        await runtime.check_sign_in()
        assert len(looks) == 1 and runtime.known_sign_in() == "signed_out"
        mtime[0] = 2
        await runtime.check_sign_in()
        await runtime.check_sign_in(max_age=0)
        assert len(looks) == 3
    finally:
        await runtime.close()


def remembering(state, *, fresh=None):
    async def check(self, *, max_age=1800):
        result = fresh if fresh is not None and max_age == 0 else state
        self.remember_sign_in(result, "reason")
        return result, "reason"
    return check


async def never_called(self, name, arguments, progress=None):
    raise AssertionError("a signed-out site must not run the tool")


async def test_a_signed_out_site_answers_with_the_link_before_anything_runs(app, monkeypatch):
    monkeypatch.setattr("conduit.runtime.Runtime.check_sign_in", remembering("signed_out"))
    monkeypatch.setattr("conduit.runtime.Runtime.call", never_called)
    async with create_connected_server_and_client_session(app.state.gateway) as session:
        read = (await session.call_tool(READ, {"id": "r1"})).content[0].text
        delete = (await session.call_tool(DELETE, {"id": "r1"})).content[0].text
        found = (await session.call_tool("seira_find", {"request": "get a record on example.com"})).structuredContent
    signed_out = r"You're signed out of example\.com\. Sign in here, then ask again: " + SIGN
    assert re.fullmatch(signed_out, read) and re.fullmatch(signed_out, delete)
    assert found["status"] == "sign_in_required" and re.fullmatch(signed_out, found["message"])
    assert app.state.audit.tail()[-1]["outcome"] == "signed_out"


async def test_a_build_waits_for_a_sign_in_instead_of_spending(app, monkeypatch):
    built = []

    async def compile_now(runtime, *, progress=None, brief=None, **kwargs):
        built.append(brief)
        return {"status": "ready"}

    monkeypatch.setattr("conduit.app.compile_connector", compile_now)
    monkeypatch.setattr("conduit.app.model_settings", lambda: {"available": True})
    (app.state.store.home / "auth").mkdir(exist_ok=True)
    (app.state.store.home / "auth" / "demo-1.session").write_bytes(b"JSON\n{}")
    request = {"site": "https://example.com", "request": "archive a record"}
    with TestClient(app):
        async with create_connected_server_and_client_session(app.state.gateway) as session:
            monkeypatch.setattr("conduit.runtime.Runtime.check_sign_in", remembering("signed_out"))
            out = (await session.call_tool("seira_build", request)).structuredContent
            monkeypatch.setattr("conduit.runtime.Runtime.check_sign_in", remembering("unsure"))
            unsure = (await session.call_tool("seira_build", request)).structuredContent
            manifest = app.state.store.get("demo-1")
            manifest.findings.append(signed_in_once({}, {}))
            app.state.store.write(manifest)
            app.state.registry.refresh()
            queued = (await session.call_tool("seira_build", request)).structuredContent
            for _ in range(40):
                if built:
                    break
                await asyncio.sleep(0.05)
    assert out["status"] == "sign_in_required" and re.fullmatch(
        r"You're signed out of example\.com\. Sign in here, then ask again: " + SIGN, out["message"])
    assert re.fullmatch(r"Seira can't tell whether you're still signed in to example\.com\. Sign in here to make "
                        r"sure, then ask again: " + SIGN, unsure["message"])
    assert queued["status"] == "queued" and built == ["archive a record"]


async def test_a_failure_with_no_sign_of_a_sign_out_is_checked_again_before_a_repair(app, monkeypatch):
    async def broken(self, name, arguments, progress=None):
        raise ProgramError("javascript_failed", "missing user info")

    monkeypatch.setattr("conduit.runtime.Runtime.call", broken)
    monkeypatch.setattr("conduit.runtime.Runtime.check_sign_in", remembering("signed_in", fresh="signed_out"))
    monkeypatch.setattr("conduit.app.model_settings", lambda: {"available": True})
    async with create_connected_server_and_client_session(app.state.gateway) as session:
        text = (await session.call_tool(READ, {"id": "r1"})).content[0].text
        listed = {t.name for t in (await session.list_tools()).tools}
    assert re.fullmatch(r"You're signed out of example\.com\. Sign in here, then ask again: " + SIGN, text)
    assert READ in listed and not app.state.jobs
    assert all(c.get("status") != "degraded" for c in app.state.store.get("demo-1").capabilities)


class Window:
    """The headed sign-in window: one tab whose page moves on each time it is read."""

    def __init__(self, manifest, steps, settled=("unsure", "nothing shows")):
        self.manifest, self.steps, self.reads, self.settled = manifest, list(steps), 0, settled
        self.page = self
        self.browser = self.context = self

    @property
    def pages(self):
        return [self]

    def is_connected(self):
        return True

    def is_closed(self):
        return False

    @property
    def url(self):
        return self.steps[0][0]

    async def evaluate(self, script):
        if self.reads and len(self.steps) > 1:
            self.steps.pop(0)
        self.reads += 1
        return self.steps[0][2]

    async def title(self):
        return self.steps[0][1]

    async def start(self):
        return None

    async def check_sign_in(self, *, expect_sign_in=False):
        return self.settled

    async def wait_for_load_state(self, *args, **kwargs):
        return None


LANDING = ("https://forms.example/", "Forms - free surveys", SIGN_IN_BUTTON)
LANDING_LOADING = ("https://forms.example/", "Forms - free surveys", PLAIN)
MICROSOFT = ("https://login.other.example/", "Sign in", PASSWORD)
APP = ("https://forms.example/Pages/Design", "Forms", PLAIN)


@pytest.fixture
def no_waiting(monkeypatch):
    sleep = asyncio.sleep

    async def no_wait(seconds):
        await sleep(0)

    monkeypatch.setattr(asyncio, "sleep", no_wait)
    monkeypatch.setattr("conduit.login.STEADY", 0.05)


async def test_the_sign_in_window_waits_for_a_page_that_is_really_signed_in(no_waiting):
    before = {"url": LANDING[0], "title": LANDING[1], **SIGN_IN_BUTTON}
    through = Window(forms(False), [LANDING, MICROSOFT, APP])
    stuck = Window(forms(False), [LANDING])
    loading_again = Window(forms(False), [LANDING, MICROSOFT, LANDING_LOADING])
    known_app = Window(forms(), [APP])
    assert await watch_for_sign_in(through, before, 5)
    assert not await watch_for_sign_in(stuck, before, 0.2)
    assert not await watch_for_sign_in(loading_again, before, 0.2)
    assert await watch_for_sign_in(known_app, {}, 0.2)


async def test_the_check_after_a_sign_in_never_takes_the_page_it_began_on():
    before = {"url": LANDING[0], "title": LANDING[1], **SIGN_IN_BUTTON}
    app_page = Window(forms(False), [APP])
    assert (await confirm(app_page, before, seen=True))["verified"]
    assert not (await confirm(app_page, before, seen=False))["verified"]
    assert not (await confirm(Window(forms(False), [APP], ("signed_out", "a sign-in control is visible")),
                              before, seen=True))["verified"]
    still_loading = await confirm(Window(forms(False), [LANDING_LOADING]), before, seen=True)
    assert not still_loading["verified"]
    assert (await confirm(Window(forms(), [APP], ("signed_in", "the app")), before))["verified"]


def test_a_sign_in_with_the_same_page_before_and_after_hides_no_markers():
    manifest = forms()
    manifest.findings.append(signed_in_once({"url": LANDING[0], "title": LANDING[1]},
                                            {"url": LANDING[0], "title": LANDING[1]}))
    assert sign_in_pages(manifest) == sign_in_pages(forms())


@pytest.mark.integration
async def test_a_real_loading_page_is_judged_only_once_it_settles(local_site, tmp_path, monkeypatch):
    url, _ = local_site
    monkeypatch.setattr("conduit.browser.DOUBT_HOLDS", 1.0)
    manifest = Manifest(id="landing", name="Landing", kind="website", target=url + "/landing",
                        allowed_origins=[url], allow_private=True, tools=browser_tools())
    manifest.findings.append(signed_in_once({"url": url + "/landing", "title": "Landing"},
                                            {"url": url + "/app", "title": "App"}))
    browser = BrowserSession(manifest, tmp_path)
    try:
        signed_out = await browser.check_sign_in()
        await browser.context.add_cookies([{"name": "test_session", "value": "present", "url": url}])
        signed_in = await browser.check_sign_in()
    finally:
        await browser.close()
    assert signed_out[0] == "signed_out" and signed_in[0] == "signed_in"


@pytest.mark.parametrize("headed, manual_login, expected", [
    (True, True, ["chrome", None, "msedge"]),
    (False, False, ["msedge", "chrome", None]),
])
async def test_a_person_signs_in_through_chrome_never_first_through_edge(tmp_path, monkeypatch, headed,
                                                                         manual_login, expected):
    tried = []

    class Chromium:
        async def launch(self, *, channel, headless):
            tried.append(channel)
            raise RuntimeError("not installed")

    class Playwright:
        chromium = Chromium()

        async def start(self):
            return self

        async def stop(self):
            return None

    monkeypatch.delenv("CONDUIT_BROWSER_CHANNEL", raising=False)
    monkeypatch.setattr("conduit.browser.os.name", "nt")
    monkeypatch.setattr("conduit.browser.async_playwright", Playwright)
    browser = BrowserSession(forms(), tmp_path, headed=headed)
    browser.manual_login = manual_login
    with pytest.raises(RuntimeError):
        await browser.start()
    assert tried == expected
