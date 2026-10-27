# Problems Encountered

Only problems / blockers are logged here (date only, no time). General progress goes in
`journal.md`. Each entry: what happened, cause, and status (`[OPEN]` / `[resolved]` / `[note]`).
**Newest entries first.**

Dates are local (America/Chicago).

Items with no single discovery date live under **Carried over from research** at the bottom; the
full account of each is in `docs/research/problems_encountered.md`.

## 2026-10-27

- **[resolved] A build on a signed-out Microsoft Forms spent 3 model passes.** Asked to create a
  form, Claude found the `forms` tools hidden and queued a build; it learned on Microsoft's sign-in
  page until Iminabo cancelled it. Three causes. (1) The Forms tools were hidden again: the old
  gateway, still running with its own copy of the connector, wrote it back over the hand repair at
  the 07:00 daily check. (2) The pre-build check looked once, before the page settled: Forms first
  paints its own page, checks the sign-in in a hidden frame, and only then shows its public landing
  page with a "Sign in" button. No password field, no redirect, so nothing fired. (3) The 07:00
  daily check had found Forms signed out, and nothing used that. Fixed: one sign-in check
  (`BrowserSession.check_sign_in`) watches the start page settle for up to 15 s and also reads a
  "Sign in" button that stays, plus the start page as the last sign-in saw it signed out and signed
  in. It runs on first use after a start, before every build or repair, and before a tool is
  withdrawn; its answer is kept 30 minutes and used by calls, `seira_find` and `seira_build`. Run
  against the real Forms with the expired session it answers signed out in about 5 s. The Forms
  tools were restored again with the gateway stopped (backup `connector.before-undegrade-2.json`).

- **[resolved] The Forms sign-in window vanished about a second after the sign-in (10:29, 10:43,
  10:44).** The sign-in record (`signin.jsonl`) showed one tab going forms.cloud.microsoft/ ->
  login.microsoftonline.com authorize -> back to /Pages/DesignPagev2.aspx, signed in, and then the
  browser gone while that page loaded. Cause, already known in research (problems log, "Use Chrome,
  not Edge"): the sign-in window opened in the installed Edge, whose signed-in profile takes over a
  Microsoft sign-in and closes the window it came from; the 2026-10-18 sign-in that worked ran with
  `--channel chrome`, and the link flow never carried that over. Fixed: a person's sign-in window
  opens in Chrome, then Playwright's Chromium, and Edge only if neither exists
  (`CONDUIT_BROWSER_CHANNEL` still overrides); the session it saves works in headless Edge, as on
  2026-10-18. Changes: the person now says when they are done
  ("I've signed in" on the link page, as `conduit login` asked for Enter on 2026-10-18); the window
  closes only after the saved cookies open the site signed in in a separate browser; a window that
  vanishes by itself is named as such; and `<home>/signin.jsonl` records each sign-in's steps and
  the pages the window went through (site, path and title only), so the next attempt shows the cause.

- **[resolved] The sign-in window still closed while Forms was loading (10:20).** Judging from
  the page when a sign-in has finished failed twice; the saved session was tested only after the
  window had closed. Fixed for every site: when the window looks signed in, Seira saves its cookies
  so far, opens the site with only those in a separate hidden browser, and closes the window only
  when that browser lands signed in; otherwise the window stays open and Seira tries again 5 s
  later. The status page says "Leave the sign-in window open" meanwhile.

- **[resolved] The sign-in link said "You're signed in" when Forms was not.** The first live link
  sign-in (10:07) was confirmed by the rule "went through Microsoft and came back with no sign-in
  prompt", but the page it came back to, and the page the check then reopened, was Forms' public
  landing page while it was still loading, before its "Sign in" button appeared. The window most
  likely closed in the middle of the sign-in, so the saved session was never signed in, and the
  next call said signed out again. The bad record (same page before and after) also hid the
  signed-in and signed-out pages learned on 2026-10-18. Fixed: the window waits for the known
  signed-in page, or for a page that stays 5 s with no sign-in prompt and is neither the page the
  window opened on nor the known signed-out page; the check after the window closes uses the
  settled sign-in check and never accepts the page the sign-in began on; markers come from the
  newest verified sign-in whose pages differ; the status page says "Checking the sign-in" meanwhile.

- **[resolved] The sign-in link's button answered "This action must originate from the local
  Conduit dashboard."** Every Seira page sent `Referrer-Policy: no-referrer`, and under that policy
  a browser sends `Origin: null` with a form's POST, which the forged-request check refuses. The
  test client sends no Origin at all, so the tests passed. Fixed: the link pages use `same-origin`,
  a refused link POST shows a plain page instead of JSON, and
  `test_links.py::test_the_link_buttons_work_in_a_real_browser` presses both buttons in a real
  browser (it fails with the old header).

- **[resolved] The sign-in link could never confirm a Forms sign-in.** It waited for a sign-out
  control or a password form that disappears; Forms shows neither (the password is typed on
  Microsoft's site). A sign-in now also counts once the person went through a sign-in page or
  another site and is back on the site with no sign-in prompt.

- **[resolved] An expired Microsoft sign-in became a paid repair and a raw error.** Forms
  redirected to `login.microsoftonline.com`, not in the connector's allowed or login origins, so
  the browser aborted the redirect; the JavaScript program found no user info and failed
  `javascript_failed`. Keep read that as a broken program: it hid the Forms tools and queued a
  repair that spent 3 model passes before Iminabo cancelled it. The user saw the internal code, a
  stack trace with escape characters, `degraded: true` and a job id. Fixed: the runtime judges the
  sign-in when a program fails, while it still holds the browser (aborted off-site navigation,
  off-site redirect, 401, a page left on another site or a login origin, a password field); a
  sign-out answers with a sign-in link and never degrades; the compiler and the daily check use
  the same judgement, so a signed-out repair stops before any model call. The Forms tools were
  restored by hand (backup `connector.before-undegrade.json`).

- **[resolved] Users saw internals and developer instructions.** Gateway errors carried internal
  codes, stack traces, raw Playwright and pydantic text, connector and job ids, `python -m conduit`
  commands and `.env`. Tool descriptions and schemas carried page titles and compiler-model text,
  a path for instructions planted in a website to reach the client. Fixed with `conduit/messages.py`
  (one sentence per situation, references into `errors.jsonl`) and projected receipts, schemas
  and meta-tool outputs.

- **[resolved] The agent could approve its own delete.** The `confirm_token` came back in the
  same message the agent read. Replaced by Seira's own elicitation question or an approval link
  whose page only a click approves.

- **[resolved] The Desktop hub skipped the gateway's rules.** It proxied the dashboard's `/call`,
  so blocks, delete approval, withdrawn tools, the audit log and the new messages did not apply.
  It is now a stdio bridge to `/mcp`.

- **[OPEN] Two write-grounding tests fail: a learned program that writes to a path never observed
  on the site is accepted.** `test_write_grounding.py::test_steps_create_to_an_unobserved_path_is_rejected_before_it_is_sent`
  and `::test_javascript_create_to_an_unobserved_path_is_blocked_by_the_runtime_backstop` end
  `ready` instead of `incomplete`. They fail the same way on `6c6af50`, before today's changes;
  never run in this repo until today. This is the guard that keeps an invented write endpoint off
  a real account, so it needs its own investigation.

- **[OPEN] `test_document_compiler.py::test_failed_document_navigation_is_reconciled_by_fresh_full_export`
  fails** (`KeyError: 'creation_no_effect_confirmed'`), also on `6c6af50`.

- **[note] Server-side redirects never reach Playwright's route handler**, so a 302 to a login page
  is caught from the response event instead; confirmed by the `/offsite` and `/protected` routes in
  `tests/test_signin.py`.

## 2026-10-19

- **[resolved] Windows blocked the venv's Python: "An Application Control policy has blocked this
  file".** Smart App Control is on (it reads 1 in `HKLM\SYSTEM\CurrentControlSet\Control\CI\Policy`,
  `VerifiedAndReputablePolicyState`); it ran the venv on Oct 18 and blocked it on Oct 19, most
  likely the switch from evaluation to enforcement. It refuses unsigned programs without a
  reputation. The venv was made from uv's downloaded CPython 3.12, which ships no signed launcher,
  so uv wrote its own unsigned `python.exe`. Fixed by rebuilding the venv from the python.org
  Python 3.14 (`uv venv --python-preference only-system`), whose launcher is signed by the Python
  Software Foundation; `.python-version` is now 3.14 and `requirements.lock` was regenerated.
  Smart App Control stays on: once off, it cannot be turned back on without resetting Windows.

- **[note] Smart App Control can block a compiled extension the first time it loads.** On the
  rebuilt venv the first `import jsonschema` failed on `rpds` (a Rust `.pyd`) with the same
  message; a minute later every one of the venv's 44 compiled modules loaded, and the fast suite
  passed (192 passed, 1 skipped). If an import fails with that message right after an install,
  run it again before changing anything.

## 2026-10-18 (finishing Tier 0)

- **[resolved] `browser_close` saved state before releasing, so signed-out cookies could overwrite a
  valid login.** `BrowserSession.save` now refuses to write over a stored session when the page
  shows a password field or a sign-in control with no sign-out; the checkpoints taken during a
  manual login get the same guard. A refusal comes back as `session_warning` on browser tools.

- **[resolved] A refreshed session was invisible to a running server.** The session file's mtime is
  kept from load time; a file rewritten by a sign-in elsewhere restarts the browser context before
  the next call.

- **[resolved] One client at a time, with no handoff.** The exclusive file lock is a lease with a
  five-minute expiry (`session_lock.py`), renewed on every call and every minute while a call or
  compile runs. An idle or dead holder loses it; `conduit login` can take the session over; the old
  holder notices on its next call, closes without saving and re-acquires, or reports busy.

- **[resolved] `last_error` lived in memory only.** A program-breaking error marks the resource
  `degraded` in `connector.json`, hides its tools, notifies clients and queues a repair
  (`keep.py`); a missing record, a stale token or a sign-out never degrades.

- **[note] The daily check makes no read.** Verification deletes its disposable records and keeps
  no id, so the check judges each stored session (signed in or not) and stops there. Measured on
  the research home: four sites in 11.8 s.

- **[note] `users.json` is off until the first `conduit users add`.** From then on every `/mcp`
  request needs `Authorization: Bearer <key>`, including an existing `claude mcp add seira`
  (re-add it with `--header`).

- **[note] The checker's model call is unmeasured.** It uses the compiler's provider at low effort
  with 300 output tokens and runs only when the deterministic match cannot decide; its cost per
  call has not been recorded.

## 2026-10-18 (the front door)

- **[resolved] The connector `notes` field shipped build instructions to MCP clients.** Three serve
  paths carried it: the per-connector server's instructions (`mcp_server.py`), its
  `conduit://discovery` resource, and `GET /api/connectors/{id}`, which the hub reads. All three
  now omit it; the gateway never reads it. A gateway build passes its request to the compiler as a
  `brief` argument instead of writing it to the manifest. `test_tool_results.py`,
  `test_dashboard.py` and `test_gateway.py` assert every served surface is free of a marker note.

- **[resolved] The front door did not exist.** `conduit ui` now also serves a Streamable HTTP MCP
  endpoint at `/mcp` (`conduit/gateway.py`, mounted in `app.py`), listing verified program tools
  across connectors from an mtime-refreshed index (`conduit/registry.py`) and running them in
  process. Claude Code connects with `claude mcp add --transport http seira http://127.0.0.1:4455/mcp`.

- **[resolved] `/api/connectors/{id}/call` dropped `object_id`, `postcondition` and
  `execution_observations` from a failed write**, the fields `mcp_check.verify_export` reads to
  decide whether a create is uncertain; the hub proxies that route, so it lost them too. One
  function now shapes every surface's error (`conduit/tool_results.py`).

- **[note] A cached runtime could keep running a program the CLI had just replaced.** `runtime_for`
  now compares `connector.json`'s mtime and reloads when nothing is running on that connector.
  Covered by `test_runtime_reloads_when_connector_json_changes`.

- **[OPEN] Progress heartbeat not implemented.** UI-driven tools take 12 to 48 s and the client's
  tool-call timeout is unmeasured. The planned 10-second `progress` notification was cut for time;
  measure the timeout on the first UI-driven run before deciding whether it is needed.

- **[OPEN] HubSpot `contacts_read` of a nonexistent id returns a record with null fields instead of
  `object_missing`.** Found on the first live call through the gateway (17.5 s, cold browser): the
  receipt broke the tool's own output schema (`email` must be a string) and the SDK client threw
  the whole result away. Dropping the structured part is no fix either: the client discards a
  non-error result whose tool declares a schema but returns no structured content. The gateway
  now reports such a receipt as an error, code `receipt_schema_mismatch`, carrying the receipt
  (`test_a_receipt_that_breaks_its_schema_is_an_error_that_keeps_the_receipt`); the client only
  validates non-error results. On id `000000000` the name it read back was the title of HubSpot's
  login page, so the program does not check that it landed on a record page. The read program
  needs that check and a not-found result; it is a research-era connector, not gateway code.

- **[note] The research HubSpot connector is named "Loading https://app.hubspot.com/login".**
  Discovery took the page title while the login page was loading, and the gateway now shows that
  name in every HubSpot tool description. Rename the manifest; discovery should ignore
  loading-state titles.

- **[note] `seira_build` refused to see an existing HubSpot tool** because the user said
  `app.hubspot.com` and the connector's target is `app-na2.hubspot.com`. Site matching now falls
  back to the domain label when it names exactly one known site.

- **[note] Browser-based MCP clients are refused by the Origin rule.** The endpoint allows no
  `Origin` (Claude Code, a Node process) or a localhost one; MCP Inspector in a browser gets 403.
  Intended.

## 2026-10-18 (seeding the production repo)

- **[resolved] The fast suite errored on a fresh clone: 13 fixture setup errors.** Cause: pytest's
  `--basetemp artifacts/pytest-run` needs `artifacts/` to exist, and the research repo had it only
  because of its run reports. Fixed by tracking `artifacts/.gitkeep` with a `.gitignore` exception;
  146 passed, 1 skipped afterwards.

- **[resolved] Two test modules import from `scripts/`, which was not copied.** `test_ladder.py`
  loads `ladder.py` and `verify_rest_export.py` by path; `test_forms_observer.py` imports
  `verify_forms_export` and `verify_forms_workflow`. The five files (plus `ladder.json`) were
  copied; the other thirteen research scripts were not.

- **[resolved] No venv of its own at first.** The first test run used the research venv's Python
  3.14.6. Fixed the same day: `uv venv` (uv 0.12.0, Python 3.12.13, pinned in `.python-version`),
  `uv pip install -e ".[dev]"`, Playwright's Chromium. The fast suite passes under it: 146 passed,
  1 skipped. The `requirements.lock` still comes from the research repo and has not been
  regenerated here.

- **[resolved] The design doc's drawings did not survive export.** `docs/SYSTEM_DESIGN.md` carries text
  versions of the loop and the Tier 0 layout, and the drawn versions are now in the repo as
  `docs/design/jit-loop.png` and `docs/design/tier0.png`, rendered from the Claude Doc.

## Carried over from research (still open)

Each of these is described in full, with its date, in `docs/research/problems_encountered.md`. The
first five blocked every tier in `docs/SYSTEM_DESIGN.md` and are closed as of 2026-10-18.

- **[resolved 2026-10-18] `browser_close` saved state before releasing, so signed-out cookies could
  overwrite a valid login.** See the Tier 0 entry above.
- **[resolved 2026-10-18] A refreshed session was invisible to a running MCP server.** See above.
- **[resolved 2026-10-18] One client at a time, with no handoff.** A lease with expiry; see above.
- **[resolved 2026-10-18] The connector `notes` field shipped build instructions to MCP clients.**
  See the front-door entry above.
- **[resolved 2026-10-18] The front door did not exist.** See the front-door entry above.
- **[OPEN] `"model_calls": 0` is a literal, not a measurement** (`conduit/programs.py:812`). The
  guarantee is structural today; make it measured.
- **[OPEN] The compiler prompt (~220 lines, `conduit/compiler.py`) is unversioned and untested.**
- **[OPEN] Truncated model output gets up to 8 closing braces added silently.**
- **[OPEN] The single-operation server hardcodes `update`** (`conduit/single_operation.py`).
- **[OPEN] Leaked browser processes** (82 logged on Oct 3); no reaper.
- **[OPEN] The form parser discards checkbox, radio, file and password forms**
  (`conduit/discovery.py`).
- **[OPEN] Every connector bundle carries a full copy of the package under `_runtime`**
  (`conduit/generator.py`).
- **[OPEN] No independent observer for Snipe-IT or Microsoft Forms** beyond the MCP gate.
- **[OPEN] A burned API key is still in the shared Drive doc.** Rotate before anything is hosted.
- **[OPEN] Unassisted canvas discovery has never completed.** Parked by design (website-first).
