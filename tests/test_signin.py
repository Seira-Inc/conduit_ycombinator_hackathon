import re

import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from test_gateway import READ

from conduit.browser import BrowserSession, browser_tools
from conduit.compiler import compile_connector
from conduit.models import Manifest
from conduit.programs import ProgramError
from conduit.runtime import Runtime

SIGN_OUT = {"password_inputs": 0, "identity_inputs": 0, "sign_in_controls": 0, "sign_out_controls": 1, "text": ""}
PASSWORD = {"password_inputs": 1, "identity_inputs": 1, "sign_in_controls": 1, "sign_out_controls": 0, "text": ""}
EMAIL_FIRST = {"password_inputs": 0, "identity_inputs": 1, "sign_in_controls": 1, "sign_out_controls": 0, "text": ""}
PLAIN = {"password_inputs": 0, "identity_inputs": 0, "sign_in_controls": 0, "sign_out_controls": 0, "text": ""}


class Page:
    def __init__(self, url, facts):
        self.url, self.facts = url, facts

    def is_closed(self):
        return False

    async def evaluate(self, script):
        return self.facts

    async def title(self):
        return "Page"

    async def wait_for_load_state(self, *args, **kwargs):
        return None


def session(tmp_path, url="https://example.com/app", facts=PLAIN, **signals):
    manifest = Manifest(id="demo", name="Demo", kind="website", target="https://example.com",
                        allowed_origins=["https://example.com"], login_origins=["https://login.example.net"],
                        tools=browser_tools())
    browser = BrowserSession(manifest, tmp_path)
    browser.context, browser.page, browser.login_return_wait = object(), Page(url, facts), 0
    for name, value in signals.items():
        setattr(browser, name, value)
    return browser


@pytest.mark.parametrize("url, facts, signals, expected", [
    ("https://example.com/app", PLAIN, {"off_site": "https://login.microsoftonline.com"}, "strong"),
    ("https://example.com/app", PLAIN, {"auth_challenge": True}, "strong"),
    ("https://login.example.net/authorize", PLAIN, {}, "strong"),
    ("https://elsewhere.example/x", PLAIN, {}, "strong"),
    ("https://example.com/login", PASSWORD, {}, "strong"),
    ("https://example.com/app", SIGN_OUT, {}, "signed_in"),
    ("https://example.com/login", EMAIL_FIRST, {}, "weak"),
    ("https://example.com/app", PLAIN, {}, None),
])
async def test_sign_in_state_reads_every_signal(tmp_path, url, facts, signals, expected):
    signal, reason = await session(tmp_path, url, facts, **signals).sign_in_state()
    assert signal == expected and (reason is None) == (expected is None)


async def test_no_browser_means_no_judgement(tmp_path):
    browser = session(tmp_path)
    browser.context = None
    assert await browser.sign_in_state() == (None, None)


def raising(code, signal, *, uncertain=False):
    async def call(self, name, arguments, progress=None):
        exc = ProgramError(code, "internal detail", uncertain=uncertain)
        exc.sign_in, exc.sign_in_reason = signal, "reason"
        raise exc
    return call


SIGN = r"http://127\.0\.0\.1:4455/signin/[A-Za-z0-9_-]{22}"


@pytest.mark.parametrize("code, signal, uncertain, expected", [
    ("javascript_failed", "strong", False,
     r"You're signed out of example\.com\. Sign in here, then ask again: " + SIGN),
    ("authentication_required", "signed_in", False,
     r"You don't have access to that on example\.com\. Ask its owner for access\."),
    ("authentication_required", None, False, r"Example\.com refused this\. You may need to sign in again: " + SIGN),
    ("locator_missing", "weak", False, r"Example\.com refused this\. You may need to sign in again: " + SIGN),
])
async def test_a_sign_out_is_never_treated_as_a_broken_tool(app, monkeypatch, code, signal, uncertain, expected):
    monkeypatch.setattr("conduit.runtime.Runtime.call", raising(code, signal, uncertain=uncertain))
    monkeypatch.setattr("conduit.app.model_settings", lambda: {"available": True})
    async with create_connected_server_and_client_session(app.state.gateway) as session_:
        result = await session_.call_tool(READ, {"id": "r1"})
        listed = {t.name for t in (await session_.list_tools()).tools}
    assert result.isError and re.fullmatch(expected, result.content[0].text)
    assert READ in listed and not app.state.jobs
    assert all(c.get("status") != "degraded" for c in app.state.store.get("demo-1").capabilities)


async def test_an_uncertain_write_outranks_a_sign_out(app, monkeypatch):
    monkeypatch.setattr("conduit.runtime.Runtime.call", raising("outcome_unknown", "strong", uncertain=True))
    async with create_connected_server_and_client_session(app.state.gateway) as session_:
        result = await session_.call_tool(READ, {"id": "r1"})
    assert result.content[0].text.startswith("Seira couldn't confirm whether the record was read")


@pytest.mark.integration
@pytest.mark.parametrize("path, expected", [("/offsite", "strong"), ("/protected", "strong"), ("/session", None)])
async def test_real_redirects_and_sign_in_pages_are_recognised(local_site, tmp_path, path, expected):
    url, _ = local_site
    manifest = Manifest(id="signals", name="Signals", kind="website", target=url + path, allowed_origins=[url],
                        allow_private=True, tools=browser_tools())
    browser = BrowserSession(manifest, tmp_path)
    try:
        await browser.start()
        signal, _ = await browser.sign_in_state()
    finally:
        await browser.close()
    assert signal == expected


@pytest.mark.integration
async def test_a_signed_out_build_stops_before_any_model_call(local_site, tmp_path):
    url, _ = local_site
    manifest = Manifest(id="signed-out", name="Signed out", kind="website", target=url + "/offsite",
                        allowed_origins=[url], allow_private=True, tools=browser_tools())
    runtime = Runtime(manifest, tmp_path)

    async def model(*args, **kwargs):
        raise AssertionError("a signed-out build must not call the model")

    try:
        report = await compile_connector(runtime, model=model, research=False)
    finally:
        await runtime.close()
    assert report["status"] == "login_required" and report["model_calls"] == 0
