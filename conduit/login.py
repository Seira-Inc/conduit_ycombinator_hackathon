"""Generic sign-in checks. The website's own page decides; no site-specific selectors live here."""
from __future__ import annotations

from contextlib import suppress

from playwright.async_api import Error as PlaywrightError

FACTS = """() => {
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
    """Poll the headed login browser until a page looks signed in, the browser closes, or time runs out."""
    import asyncio
    import time

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not (session.browser and session.browser.is_connected()):
            return False
        pages = [p for p in session.context.pages if not p.is_closed()] if session.context else []
        for page in pages:
            selector_found = None
            if selector:
                with suppress(PlaywrightError):
                    selector_found = await page.locator(selector).count() > 0
            after = await fingerprint(page)
            verified, _ = looks_signed_in(before, after, text=text, selector_found=selector_found)
            if verified:
                session.page = page
                await asyncio.sleep(3)
                return True
        await asyncio.sleep(2)
    return False


async def confirm(session, before, *, text=None, selector=None) -> dict:
    """Reopen the target through the saved session and report whether it looks signed in."""
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
    return {"verified": verified, "reason": reason, "before": _facts(before), "after": _facts(after)}
