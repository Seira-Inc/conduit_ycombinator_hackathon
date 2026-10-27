"""Generic sign-in checks. The website's own page decides; no site-specific selectors live here."""
from __future__ import annotations

import time
from contextlib import suppress
from datetime import datetime
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError

from .models import origin

TRUST_SIGN_IN = 1800
STEADY = 5.0
FACTS ="""() => {
  const visible = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const label = e => (e.innerText || e.value || e.getAttribute('aria-label') || '').trim();
  const controls = [...document.querySelectorAll('a,button,[role=button],[role=link],input[type=submit]')]
    .filter(visible);
  const signIn = /\\b(sign|log)[\\s-]?in\\b/i, signOut = /\\b(sign|log)[\\s-]?out\\b/i;
  const identity = 'input[type=email],input[autocomplete=username],input[name*=user i],input[name*=login i],'
    + 'input[name*=email i],input[id*=email i]';
  return {
    password_inputs: [...document.querySelectorAll('input[type=password]')].filter(visible).length,
    identity_inputs: [...document.querySelectorAll(identity)].filter(visible).length,
    sign_in_controls: controls.filter(e => signIn.test(label(e))).length,
    sign_out_controls: controls.filter(e => signOut.test(label(e))).length,
    text: (document.body ? document.body.innerText : '').slice(0, 4000),
  };
}"""
HINT = "pass --logged-in-text with words only a signed-in page shows"


async def fingerprint(page) -> dict:
    """Login-relevant facts about the current page, never field values."""
    facts = {"password_inputs": 0, "identity_inputs": 0, "sign_in_controls": 0, "sign_out_controls": 0, "text": ""}
    with suppress(PlaywrightError):
        facts = await page.evaluate(FACTS)
    title = ""
    with suppress(PlaywrightError):
        title = await page.title()
    return {"url": page.url, "title": title, **facts}


def looks_signed_in(before, after, *, text=None, selector_found=None):
    """Return (verified, reason). Without a marker, only a sign-out control or a vanished password form counts."""
    if text:
        found = text.casefold() in (after.get("text") or "").casefold()
        return found, (f"the page shows {text!r}" if found else f"the page does not show {text!r}")
    if selector_found is not None:
        return selector_found, ("the signed-in selector is present" if selector_found
                                else "the signed-in selector is absent")
    if after.get("password_inputs"):
        return False, "a password field is still visible"
    if after.get("sign_out_controls"):
        return True, "a sign-out control is visible"
    if before and before.get("password_inputs") and not after.get("sign_in_controls"):
        return True, "the password form is gone and no sign-in control remains"
    return False, "no sign-out control or signed-in marker was found; " + HINT


def looks_signed_out(after) -> str | None:
    """A reason when the page shows it is signed out, else None; an unreadable page is not signed out."""
    if after.get("password_inputs"):
        return "a password field is visible"
    if after.get("sign_in_controls") and not after.get("sign_out_controls"):
        return "a sign-in control is visible and no sign-out control"
    return None


def _facts(fingerprint_dict):
    return {k: v for k, v in (fingerprint_dict or {}).items() if k != "text"}


def login_finding(check: dict) -> dict:
    return {"type": "login_check", "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **check}


def needs_sign_in(manifest) -> bool:
    """Seira has seen this site ask for a sign-in. Any other site counts as signed in by default."""
    return bool(manifest.login_origins) or any(f.get("type") == "login_check" for f in manifest.findings)


def recently_signed_in(manifest, seconds=TRUST_SIGN_IN) -> bool:
    for finding in reversed(manifest.findings):
        if finding.get("type") == "login_check" and finding.get("verified") is True:
            try:
                at = datetime.strptime(finding.get("at", ""), "%Y-%m-%dT%H:%M:%S%z").timestamp()
            except ValueError:
                return False
            return time.time() - at < seconds
    return False


def _page_key(facts):
    url = (facts or {}).get("url") or ""
    if not url.startswith(("http://", "https://")):
        return None
    return origin(url), urlsplit(url).path.rstrip("/") or "/", ((facts or {}).get("title") or "").strip()


def page_matches(facts, key) -> bool:
    return key is not None and _page_key(facts) == key


def sign_in_pages(manifest) -> dict:
    """The start page as a verified sign-in saw it, "out" before and "in" after, from the newest one
    whose pages differ. "out" must be another address, since a title alone may just be loading."""
    for check in reversed(manifest.findings):
        if check.get("type") != "login_check" or check.get("verified") is not True:
            continue
        signed_in, signed_out = _page_key(check.get("after")), _page_key(check.get("before"))
        if signed_in is None or signed_in == signed_out:
            continue
        pages = {"in": signed_in}
        if signed_out is not None and signed_out[:2] != signed_in[:2]:
            pages["out"] = signed_out
        return pages
    return {}


def on_site(manifest, facts) -> bool | None:
    """True on one of the site's own pages, False on another site, None for a blank or error page."""
    key = _page_key(facts)
    return None if key is None else key[0] in manifest.allowed_origins


def no_sign_in_prompt(facts) -> bool:
    return not facts.get("password_inputs") and not facts.get("identity_inputs") and looks_signed_out(facts) is None


async def _first_visible(page, selector):
    candidates = page.locator(selector)
    for index in range(min(await candidates.count(), 12)):
        candidate = candidates.nth(index)
        if await candidate.is_visible():
            return candidate
    return None


async def scripted_login(session, *, user="", password="", login_url=None, user_label=None, pass_label=None,
                         submit=r"^(log ?in|sign ?in)$", success_text=None, click_only=False) -> dict:
    """Sign in through the website's own form (or a single demo sign-in button), then verify like a manual login."""
    import re

    await session.start()
    page = session.page
    if login_url:
        await page.goto(login_url, wait_until="domcontentloaded", timeout=25000)
    button = page.get_by_role("button", name=re.compile(submit, re.I)).first
    if click_only:
        await button.wait_for(timeout=15000)
        before = await fingerprint(page)
        await button.click()
    else:
        password_field = (page.get_by_label(pass_label, exact=True) if pass_label
                          else page.locator("input[type=password]:visible").first)
        # Single-page apps render the form after load; fingerprint once it is actually there.
        await password_field.wait_for(timeout=15000)
        before = await fingerprint(page)
        if user_label:
            user_field = page.get_by_label(user_label, exact=True)
        else:
            user_field = await _first_visible(page, "input[type=email], input[type=text], input:not([type])")
            if user_field is None:
                raise ValueError("No visible username field was found; pass a user label")
        await user_field.fill(user)
        await password_field.fill(password)
        if await button.count():
            await button.click()
        else:
            await password_field.press("Enter")
    with suppress(PlaywrightError):
        await page.wait_for_load_state("networkidle", timeout=20000)
    with suppress(PlaywrightError):
        await page.wait_for_function("() => !document.querySelector('input[type=password]')", timeout=20000)
    if success_text:
        with suppress(PlaywrightError):
            await page.get_by_text(success_text).first.wait_for(timeout=15000)
    await session.save()
    after = await fingerprint(page)
    verified, reason = looks_signed_in(before, after, text=success_text)
    return {"mode": "scripted", "verified": verified, "reason": reason, "before": _facts(before), "after": _facts(after)}


async def watch_for_sign_in(session, before, seconds, *, text=None, selector=None) -> bool:
    """Poll the headed login browser until a page looks signed in, the browser closes, or time runs out.
    Without a sign-out control, a page counts after a sign-in page or another site once it has stayed
    STEADY seconds with no prompt and is neither the opening page nor the known signed-out page."""
    import asyncio

    deadline = time.monotonic() + seconds
    markers = sign_in_pages(session.manifest)
    not_signed_in = {key for key in (markers.get("out"), _page_key(before)) if key is not None}
    went_through_sign_in = bool((before or {}).get("password_inputs"))
    steady = {}
    # Where the window went, for the local sign-in record: each page's site, path and title as it
    # changes, with how many tabs were open. Never a query string, a field value or a cookie.
    path, session.watch_note = [], {}

    def note(ended, **detail):
        session.watch_note = {"ended": ended, "went_through_sign_in": went_through_sign_in, "path": path[-30:],
                              **detail}

    while time.monotonic() < deadline:
        if not (session.browser and session.browser.is_connected()):
            note("window closed")
            return False
        pages = [p for p in session.context.pages if not p.is_closed()] if session.context else []
        for page in pages:
            selector_found = None
            if selector:
                with suppress(PlaywrightError):
                    selector_found = await page.locator(selector).count() > 0
            after = await fingerprint(page)
            key = _page_key(after)
            if not path or path[-1][:3] != (list(key) if key else [None, None, None]):
                path.append([*(key or (None, None, None)), len(pages)])
            if after.get("password_inputs") or on_site(session.manifest, after) is False:
                went_through_sign_in = True
            verified, rule = looks_signed_in(before, after, text=text, selector_found=selector_found)
            if not verified and text is None and selector is None:
                if page_matches(after, markers.get("in")):
                    verified, rule = True, "the known signed-in page"
                elif (went_through_sign_in and on_site(session.manifest, after) and no_sign_in_prompt(after)
                      and key not in not_signed_in):
                    if steady.get(id(page), (None,))[0] != key:
                        steady[id(page)] = (key, time.monotonic())
                    verified = time.monotonic() - steady[id(page)][1] >= STEADY
                    rule = "a page that stayed with no sign-in prompt"
                else:
                    steady.pop(id(page), None)
            if verified:
                note(rule)
                session.page = page
                await asyncio.sleep(3)
                return True
        await asyncio.sleep(2)
    note("timed out")
    return False


async def confirm(session, before, *, text=None, selector=None, seen=False) -> dict:
    """Reopen the target through the saved session and report whether it looks signed in. Without a
    marker the site's settled start page decides; `seen` (the person signed in moments ago) lets a page
    that shows nothing either way count, unless it is the page the sign-in began on."""
    await session.start()
    page = session.page
    with suppress(PlaywrightError):
        await page.wait_for_load_state("networkidle", timeout=15000)
    selector_found = None
    if selector:
        with suppress(PlaywrightError):
            await page.locator(selector).first.wait_for(timeout=15000)
        selector_found = await page.locator(selector).count() > 0
    elif text:
        with suppress(PlaywrightError):
            await page.get_by_text(text).first.wait_for(timeout=15000)
    after = await fingerprint(page)
    verified, reason = looks_signed_in(before, after, text=text, selector_found=selector_found)
    if not verified and text is None and selector is None:
        state, why = await session.check_sign_in(expect_sign_in=True)
        after = await fingerprint(session.page)
        if state == "signed_in" or (state == "unsure" and seen and _page_key(after) != _page_key(before)):
            verified, reason = True, why
    return {"verified": verified, "reason": reason, "before": _facts(before), "after": _facts(after)}
