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

## 2026-10-27 — The two test fixes merged; the live delete works

Iminabo's live delete of the "Seira test" form worked, approval included. The two fix branches are
merged into `imi` and `main`: `fix/failed-family-not-ready` (a resource that failed every check now
stays `incomplete` instead of `partial`, and the document no-effect test presses Enter instead of
Esc) and `fix/operation-import-hub-tools` (the hub import test expects the gateway's `seira_*`
tools). They cleared the four known integration failures; details in `problems_encountered.md`.

## 2026-10-27 — Seira checks the sign-in itself before it spends anything

**Why.** Iminabo's live test of Forms ended in a paid build learning on Microsoft's sign-in page.
Seira checked the sign-in at three points (daily, before a build, after a failure), and each one
missed or went unused (see `problems_encountered.md`, 2026-10-27). He asked for a check on every
cold start, made a default for every website, with sites that have no sign-in counting as signed
in. Decision: Seira runs it itself rather than giving the agent a tool to call, since an agent can
skip a call and one tool per site would double the tool list.

**What changed.**
- `BrowserSession.check_sign_in` opens the start page and watches it settle (15 s for a site that
  has asked for a sign-in, 3 s for one that never has). It answers signed in, signed out or unsure.
  A "Sign in" button counts only once it has stayed 3 s; single-page apps flash one while loading.
- The signed-in and signed-out start pages come from the last verified sign-in: every
  `login_check` now records when it happened, and its before and after pages become markers when
  they are different pages.
- `Runtime.check_sign_in` keeps the answer 30 minutes, drops it when the session file changes, and
  every call updates it. The gateway uses it before running a tool (signed out: the link, nothing
  runs), in `seira_find`, before `seira_build` (signed out or unsure: the link, no job), and before
  withdrawing a tool. The compiler's pre-model gate and the daily check use the same check.
- The sign-in link and dashboard sign-in confirm sites with no sign-out button.
- Claude is told not to show job numbers to the user.

**Then the live sign-in, four attempts.** Each failure is in `problems_encountered.md`:
- The link page's button was refused: under `Referrer-Policy: no-referrer` a browser sends
  `Origin: null` with a form's POST. The link pages now use `same-origin`.
- Seira twice decided the sign-in had finished while Forms was still loading and closed the window
  early. Guessing from the page was dropped as the way to finish: the person now presses
  "I've signed in" (as `conduit login` asked for Enter on 2026-10-18), and the window closes only
  after the cookies it saved open the site signed in, in a separate hidden browser.
- The window then vanished by itself right after Microsoft sent the person back to Forms. A new
  local record (`<home>/signin.jsonl`: steps and pages by site and path, never values) showed the
  sign-in had worked; the installed Edge closed the window. Research had already found this ("Use
  Chrome, not Edge"; the 2026-10-18 sign-in ran with `--channel chrome`). Sign-in windows now open
  in Chrome, then Playwright's Chromium, Edge last.
- With that, the sign-in and the call went through: Seira created the form "Seira test".

**For later.** `come_back_to.md` is new: things that work today but need a proper answer, starting
with which browser a person can sign in with on a machine without Chrome.

**Tests.** Fast suite 237 passed, 1 skipped (was 216); `tests/test_sign_in_check.py` covers each
judgement, the cache, the gateway answers, the browser chosen for a sign-in and a real browser on a
Forms-like loading page; `tests/test_links.py` presses the link buttons in a real browser, keeps
the window open until the saved cookies work, and takes the person's "I've signed in".
Integration 91 passed, 4 failed: the four already fixed on `fix/failed-family-not-ready` and
`fix/operation-import-hub-tools`, failing the same way, not yet merged here. Against the real Forms
with the expired session the check answers signed out in about 5 s.

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

## 2026-10-27 — messages a lawyer can read, and sign-outs that never break a tool

**Why.** Iminabo's first Forms test hit an expired Microsoft sign-in. Seira read it as a broken
program, hid the Forms tools, started a paid repair that could never work, and showed him a raw
error with a stack trace, `degraded: true` and a job id. His objections: users must never see
internals, they confuse a lawyer and inform a malicious agent; and nothing a user sees may ask
for a terminal. An independent review of every message the gateway and the hub send found the
same problem across the board (see `problems_encountered.md`, 2026-10-27).

**What changed.**
- `conduit/messages.py`: every failure is one fixed sentence filled only with values Seira
  controls. An uncertain write always says not to repeat it. Failures carry an 8-character
  reference; the redacted detail goes to `errors.jsonl` (30 days, `/api/errors/<ref>`).
- Receipts, their advertised schemas, tool descriptions, input schemas, `seira_find`,
  `seira_build`, `seira_status` and the capabilities resource show nothing internal and nothing
  written by a website or the compiler model.
- Sign-outs: the runtime judges the sign-in at the moment a program fails (strong, weak or signed
  in). A sign-out answers "You're signed out of <site>. Sign in here, then ask again: <link>" and
  never withdraws a tool; 403 with signed-in evidence answers "no access". The compiler and the
  daily check use the same judgement.
- Links (`conduit/links.py`): a sign-in link opens a page whose button opens the sign-in window
  on this computer, waits up to 10 minutes, saves the session and resumes a build that waited for
  it. A delete is approved by Seira's own elicitation question or by a link with Delete / Don't
  delete buttons; the agent can no longer approve it.
- The Desktop hub is a stdio bridge to `/mcp`, so Claude Desktop gets the same rules.
- Decisions taken with Iminabo: approval links for deletes; in Tier 0 whoever meets a sign-out
  gets the link (one person, one machine); per-person logins wait for Tier 1.

**Tests.** Fast suite 216 passed, 1 skipped (was 192). Integration: the new sign-in tests and the
hub test through the bridge pass. Two write-grounding tests and one document test fail, the same
way on the commit before these changes; logged as open.

**Forms restored.** The wrongly withdrawn `forms` capability is back to ready (backup kept). The
Microsoft session itself is still expired: the next call will answer with the sign-in link.

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
