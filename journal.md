# Conduit Journal

Running log of the production Conduit. **Newest entries first.** Append at the top.
Problems and blockers are logged separately in `problems_encountered.md`.

**Dates and times are local (America/Chicago).**

This repo started clean on 2026-10-18 from the research repo, which stays at
`startup/conduit` with no remote and 50 commits. Its journal, with the thirteen standing decisions
and the full history of who built what, is kept here as `docs/research/journal.md`; its open
problems as `docs/research/problems_encountered.md`. The design this repo builds toward is
`docs/SYSTEM_DESIGN.md` (exported from the live Claude Doc, which also holds the two drawings and
the comment threads).

## Standing decisions

The thirteen research decisions carry over (`docs/research/journal.md`, "Standing decisions from
Iminabo"). Iminabo amended and extended them on 2026-10-18, in `docs/SYSTEM_DESIGN.md` §10:

1. **Approval.** The client's own card (ChatGPT, Claude) plus Seira elicitation before delete and
   irreversible actions. Decision #4, "connected means approved", stays as the permission rule; the
   card is a safety step, not a permission.
2. **Sessions.** v1 keeps sessions on the user's PC (Tier 0). Cloud sessions are a stretch opt-in in
   v1 and the default from v2 onward.
3. **Atomic vs shortest.** Atomic families by default; the site's own composite action when its
   semantics differ, when composing would take more than five calls (a working number, still to be
   tested), or when no family can express it.
4. **Codebase engine.** Out of scope for v1. The product is the website engine only.
5. **Access.** The site's own permissions decide what a person can reach; a resource they cannot
   open gets no tool and no operation. An admin blocks or revokes one person's access to one item,
   never a whole site. A site whose terms forbid automation is still built for; the legal exposure
   is an open question for experts.
6. **Tier 0 runs local-only first.** No tunnel, no VPS: gateway and runtime on one machine until
   the loop works end to end. Customer zero is Iminabo until the others are free.

## 2026-10-19 — the venv moves to Python 3.14 so `python` runs again

Smart App Control started blocking the uv-built venv's unsigned `python.exe` (see
`problems_encountered.md`). Iminabo wants plain `python script.py` in an activated venv, as with
pip. The venv is rebuilt with uv from the python.org Python 3.14.6, whose launcher is signed;
`.python-version` is 3.14, `requirements.lock` regenerated (the fresh install moved certifi,
fastapi, starlette, httpx2 and httpcore2 up a minor or patch version). Fast suite 192 passed,
1 skipped; the real-browser session test passes. The model for builds is now configured in
`.env` (backend `anthropic`, model `claude-opus-5`, the default in `conduit/agent.py`), so
`seira_build` can run.

## 2026-10-18 — Tier 0 finished: steps 2 to 5

Iminabo's call after the hackathon: finish Tier 0 well before moving to Tier 1, since everything
but the websocket agent lifts to Tier 1 as it is. Four commits, each green on its own.

**Session bugs (§1.9, all four closed).** `session_lock.py` is a lease with a five-minute expiry,
renewed on every call and every minute while a call or compile runs; an idle or dead holder loses
it, `conduit login` can take the session over, and the old holder closes without saving and
re-acquires or reports busy. `BrowserSession.save` never writes a signed-out state over a stored
session (a password field, or a sign-in control with no sign-out), and the login checkpoints obey
the same rule. The session file's mtime is remembered at load; a sign-in elsewhere restarts the
context before the next call. The real-browser persistence test passes with the lease.

**Ask (§1.5).** A delete asks first: a form elicitation when the client advertises it, else the
call returns `confirmation_required` with a preview and a one-use `confirm_token` (ten minutes)
that the model passes back after showing the user. A decline runs nothing. Every connector tool
call is appended to `audit.jsonl`: user, tool, argument hash, approval, outcome, duration; never
values.

**Keep (§1.8).** A program-breaking error (`locator_missing`, `postcondition_failed`,
`browser_step_failed` and the like; never `object_missing`, a stale token or a sign-out) marks the
resource `degraded` in `connector.json`, drops its tools from the catalog, sends
`tools/list_changed` and queues a repair through the same gates; the caller gets `repair_job_id`,
or the exact `conduit repair` command when no model is configured. A sign-out answers with the
login steps. A daily check at `CONDUIT_DAILY_CHECK` (07:00; `off` disables; `POST /api/check`
runs it now) opens each stored session and records whether it still looks signed in. It makes no
read: verification keeps no record id.

**Company (§1.10) and sign-in to Seira.** `users.json` under the home: `conduit users add <name>
[--admin]` prints a key once with the `claude mcp add ... --header` line. From the first user on,
every `/mcp` request needs `Authorization: Bearer <key>` (401 otherwise). A block on a site or a
tool keeps it out of that user's catalog; a block on one record leaves the tool and refuses that
id. Admins see `seira_block` and `seira_unblock`. Without the file the gateway serves one local
admin, as before.

**Find (§1.2).** `seira_find` asks a small model to pick among the candidates only when the
deterministic match cannot decide (`checker.py`, low effort, 300 tokens), and says `checker: ...`
in `why`.

**Tests.** 192 passed, 1 skipped (was 176), plus the real-browser session test. Ruff clean.

**Live run over the research home.** 19 tools (the two admin tools for the local admin); the
HubSpot delete stopped at `confirmation_required` with a preview; `seira_find` deterministic; the
daily check judged all four stored sessions (Vikunja, Microsoft Forms, Excalidraw, HubSpot) signed
in within 11.8 s, which also settles the earlier HubSpot finding: the login-page title came from
HubSpot bouncing an unknown record URL, not from an expired session.

**Deferred.** The websocket agent (step 2's cloud-gateway split): the engine runs beside the
gateway on one machine, and the split returns only for a user who must keep the engine on their
own PC. The progress heartbeat. OAuth. InstaBox (a YC sponsor: full Linux VMs for agents with a
virtual desktop a person can take over, Docker inside, public HTTPS ports, a vault) is the
candidate box for Tier 1: run this Tier 0 there as one unit.

## 2026-10-18 — the Tier 0 front door

**What exists now.** `conduit ui` also serves one Streamable HTTP MCP endpoint at
`http://127.0.0.1:4455/mcp` (`conduit/gateway.py`, mounted in `app.py`). It lists every verified
program tool across connectors from an in-memory index refreshed by `connector.json` mtime
(`conduit/registry.py`), runs them in process through the dashboard's runtime map, and adds three
meta tools: `seira_find(request, site?)` (deterministic: the site is a hard filter, the verb comes
from keyword sets, the resource from token overlap), `seira_build(site, request, name?)` (answers
`exists`, `login_required` with the exact next command, or `queued` with an ETA from the median of
recorded build times) and `seira_status(job_id, cancel?)`. A finished build broadcasts
`tools/list_changed`. Tool names keep the hub's rule, `sha256(connector_id)[:8] + "__" + tool`;
the connector name and host go in the description. Client setup:
`claude mcp add --transport http seira http://127.0.0.1:4455/mcp`. Claude Desktop keeps
`conduit hub` over stdio until a tunnel exists.

**Closed on the way.** The `notes` leak (three serve paths; a gateway build passes its request to
the compiler as `brief` instead of writing it to the manifest); the `/call` route dropping the
fields `verify_export` reads (one result shape in `conduit/tool_results.py` for the per-connector
server, `/call` and the gateway); a cached runtime surviving a CLI compile (`runtime_for` reloads
on mtime change). The endpoint is bound to 127.0.0.1 with the SDK's DNS-rebinding protection
(localhost hosts and origins), accepts a request with no `Origin` because Claude Code sends none,
and keeps the 200 kB body cap.

**Tests.** 176 passed, 1 skipped (was 146): `test_tool_results.py`, `test_registry.py`,
`test_gateway.py` (in-memory client session) and `test_gateway_http.py` (a real uvicorn socket,
`streamable_http_client`, `list_changed` received). Ruff clean.

**Manual run, over the research home** (the HubSpot and Forms connectors with their DPAPI
sessions). `claude mcp get seira` reports connected. The gateway lists 17 tools with the right
annotations (reads `readOnlyHint`; update and delete `destructiveHint`).
`seira_find("read a contact in hubspot")` picks `7c8ede37__hubspot_contacts_read`;
`seira_build(site="https://app.hubspot.com", "delete a contact")` answers `exists` with the delete
tool, once site matching learned to fall back to the domain label (`app.hubspot.com` against the
connector's `app-na2.hubspot.com`). The real read ran in 17.5 to 19.7 s on a cold browser and
exposed a connector defect: for an id that does not exist, the receipt carries null fields that
break the tool's own output schema, and the SDK client discards such a result outright. The
gateway now returns it as an error, `receipt_schema_mismatch`, with the receipt attached; the
connector fix is logged in `problems_encountered.md`.

**Not done.** The progress heartbeat (cut; measure the client's timeout on the first long UI-driven
run first); the tunnel and OAuth (Tier 0 proper); the four session bugs (§1.9); `seira_build` on a
fresh site end to end, which needs a configured model (this repo's `.env` has none yet).

## 2026-10-18 — the production repo is seeded and the design is written

**The repo.** `Seira-Inc/conduit` on GitHub, private, branch `imi`; `main` holds only the initial
commit. Local folder `startup/prod/conduit`. Iminabo's call: start clean, do not carry the research
commits, copy what is necessary.

**What was copied.** The `conduit/` package (47 files, including the dashboard's `static/`), all 49
test files, `pyproject.toml`, `requirements.lock`, `.gitignore`, `.env.example`, and five scripts
the tests load (`ladder.py`, `ladder.json`, `verify_rest_export.py`, `verify_forms_export.py`,
`verify_forms_workflow.py`). Under `docs/research/`: the research `journal.md`,
`problems_encountered.md`, `HANDOFF.md` and `COMPILER_VALIDATION.md`, because the design cites
their numbers and open items.

**What was left behind.** The other thirteen research scripts, `artifacts/` (171 tracked reports
and ladder runs), the dashboard launchers (`start.ps1`, `Launch Conduit.cmd`), and the research
write-ups (`BUSINESS_TARGET.md`, `MILESTONE_AUDIT.md`, `HOLY_SLACK_REVIEW.md`, `VALIDATION.md`,
the four `CRAWL4AI_*` and `HANDOFF_CRAWL4AI.md` files). All still readable in the research repo.

**The design.** Written today as a Claude Doc, after reading every Notion page, the whole Drive
folder, and the Oct 5 and Oct 12 meeting transcripts. Spine: the just-in-time loop, tag → find →
build → verify → ask → run → confirm → keep, each step grounded in the file that does it today.
Three budget tiers: Tier 0 (free, hackathon: the existing runtime as a local agent on the user's PC,
a tiny gateway holding the front door and the registry), Tier 1 (about $50, per-company container,
cloud sessions over noVNC), Tier 2 (about $200, sandbox per call, signed fingerprints, audit).
Iminabo rejected a first draft that was architecture-first; the doc had to start from what Conduit
does and keep the loop as its spine.

**Decisions made today** are the six under Standing decisions above, plus one comment thread on the
doc: the composition threshold moved from three calls to five, to be tested.

**Tests.** The fast suite (`-m "not integration and not desktop and not live_model"`) passes in the
new folder: 146 passed, 1 skipped. First under the research venv, then under this repo's own venv,
made with `uv venv` (uv 0.12.0, Python 3.12.13, pinned in `.python-version`) and
`uv pip install -e ".[dev]"`; the team does not use pip directly. Two things had to change for the
suite to run here: `artifacts/` must exist for pytest's `--basetemp`, so `artifacts/.gitkeep` is
tracked; and the five scripts above had to come along.

**Next.** The Tier 0 front door: a Streamable HTTP MCP server on localhost that lists verified tools
from a registry and runs them through the existing runtime, plus the three meta tools
(`seira_find`, `seira_build`, `seira_status`). Nothing of it exists yet; `hub.py` over stdio is the
nearest thing.
