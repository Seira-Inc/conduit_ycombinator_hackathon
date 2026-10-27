"""Keep: what happens when a verified tool stops working, and the daily look at each site."""

from __future__ import annotations

import time
from contextlib import suppress
from datetime import datetime, timedelta

from .operation_import import verified_program

# Codes that mean the program no longer fits the site. A missing record, a stale token, a sign-out
# or an uncertain outcome is not the program's fault and never triggers a repair.
DEGRADING = {"locator_missing", "postcondition_failed", "unexpected_response", "program_failed",
             "browser_step_failed", "readback_missing", "javascript_failed", "adapter_call_failed",
             "binding_missing", "step_limit", "persistence_failed", "session_persistence_failed"}


def should_degrade(exc) -> bool:
    return exc.code in DEGRADING and not exc.uncertain


def capability_for(manifest, resource):
    return next((c for c in manifest.capabilities if c.get("resource") == resource), None)


def degrade(manifest, resource, exc) -> dict | None:
    capability = capability_for(manifest, resource)
    if capability is None:
        return None
    capability["status"] = "degraded"
    capability["degraded"] = {"code": exc.code, "message": str(exc)[:300],
                              "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    return capability


def seconds_until(clock: str, now: datetime | None = None) -> float:
    hour, minute = (int(part) for part in clock.split(":"))
    now = now or datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def daily_check(manifests, runtime_for, *, has_session, busy, write, audit=None) -> list[dict]:
    """One cheap look per site: open the stored session and judge whether it is still signed in.
    No writes to the site; a read needs a record id the verification did not keep."""
    rows = []
    for manifest in manifests:
        if manifest.kind != "website" or not any(verified_program(t) for t in manifest.tools):
            continue
        row = {"connector_id": manifest.id, "name": manifest.name}
        if not has_session(manifest.id):
            rows.append({**row, "status": "no_session"})
            continue
        runtime = runtime_for(manifest.id)
        if busy(manifest.id) or runtime.lock.locked():
            rows.append({**row, "status": "busy"})
            continue
        async with runtime.lock:
            try:
                await runtime.acquire_browser()
                await runtime.browser.start()
                with suppress(Exception):
                    await runtime.browser.page.wait_for_load_state("networkidle", timeout=10000)
                signal, reason = await runtime.browser.sign_in_state()
                status = {"strong": "signed_out", "weak": "maybe_signed_out"}.get(signal, "signed_in")
                row.update(status=status, reason=reason if status != "signed_in" else None)
            except Exception as exc:
                row.update(status="error", reason=f"{type(exc).__name__}: {str(exc)[:200]}")
            finally:
                await runtime.close()
        finding = {"type": "daily_check", "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                   "status": row["status"], "reason": row.get("reason")}
        earlier = [f for f in runtime.manifest.findings if f.get("type") == "daily_check"][-6:]
        runtime.manifest.findings = [f for f in runtime.manifest.findings if f.get("type") != "daily_check"]
        runtime.manifest.findings += earlier + [finding]
        write(runtime.manifest)
        if audit is not None:
            audit.record(user="seira", tool="daily_check", connector_id=manifest.id, verb="read",
                         arguments="", approval="scheduled", outcome=row["status"])
        rows.append(row)
    return rows
