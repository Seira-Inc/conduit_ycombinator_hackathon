# conduit

> **Hackathon snapshot, no longer developed.** This is the version of Seira built for the Y Combinator
> agent hackathon in October 2026. It gets security fixes only; development continues privately.
> To report a security problem, see [SECURITY.md](SECURITY.md).

Seira's just-in-time tool engine. Point it at a website you are signed in to; it learns the site, builds tools that create, read, update and delete records there, proves them against the live site, and serves them over MCP with no model calls at run time.

![The just-in-time loop](docs/design/jit-loop.png)

## Read first

- On the `imi` branch only, not `main`: `journal.md` (what happened and what was decided), `problems_encountered.md` (open and resolved problems), both append-only and newest first, and `come_back_to.md` (things that work for now but need a proper answer later).
- `docs/SYSTEM_DESIGN.md`: the design. The just-in-time loop in ten steps, the three budget tiers, what exists today versus what to build, and the decisions already made.
- `docs/research/`: the research phase this repo was seeded from. `journal.md` (standing decisions, newest first), `problems_encountered.md` (open items), `HANDOFF.md` (architecture and gotchas), `COMPILER_VALIDATION.md` (the measured numbers the design cites).

## Setup

Python 3.11 or newer; the repo pins 3.14 (`.python-version`). On Windows, build the venv from the python.org install, not a Python uv downloads:

```bash
uv venv --python-preference only-system && uv pip install -e ".[dev]"
python -m playwright install chromium
cp .env.example .env
```

Then activate the venv and use `python` as usual (`.venv\Scripts\activate`, then `python -m conduit ...`). Why the flag: Windows 11's Smart App Control blocks unsigned programs, and a venv made from a uv-downloaded Python gets an unsigned `python.exe` launcher. A venv made from the python.org install gets that install's signed launcher.

Fill `.env` with your own keys. It is ignored by git, as are `.conduit/` (connectors and encrypted sessions) and `artifacts/`.

## Run

```bash
python -m conduit --help
```

Commands: `login`, `create`, `compile`, `verify`, `repair`, `optimize`, `hub`, `serve`, `list`, `users`, `ui`.

## The front door (local)

`python -m conduit ui` serves the dashboard and an MCP endpoint at `http://127.0.0.1:4455/mcp`. Add it to Claude Code:

```bash
claude mcp add --transport http seira http://127.0.0.1:4455/mcp
```

It lists every verified tool across your connectors, plus `seira_find` (is there a tool for this?), `seira_build` (build one just in time; returns a job number, the build takes minutes) and `seira_status` (how is that build doing?). The endpoint accepts requests with no `Origin` header or a localhost one, so browser-based MCP clients are refused by design. Claude Desktop uses `python -m conduit hub`, a stdio bridge to this same endpoint (`--key` or `SEIRA_KEY` when there are users), so it gets every rule below.

What the endpoint enforces:

- **Users read sentences, not internals.** Every failure is one fixed sentence: signed out, no access, not found, changed since read, outcome unknown (never repeat it; read first), invalid input (naming the field), blocked, busy, being fixed, unavailable, or failed. Where it helps, the sentence ends with a reference; `GET /api/errors/<reference>` on the dashboard shows the redacted internal detail, kept in `<home>/errors.jsonl` for 30 days. Receipts carry `id`, `data`, `state_token` and nothing internal. Tool descriptions and schemas are built by Seira, never copied from the website or the compiler model.
- **Seira checks the sign-in itself before it spends anything.** The first use of a site after Seira starts, every build or repair, and every failure before a tool is withdrawn open the site's start page and watch it settle for up to 15 seconds. The answer is kept for 30 minutes and updated by every call. Signed out means the sign-in link at once, from a call, `seira_find` or `seira_build`, with nothing run and no model call. Not sure means no build starts, unless you signed in during the last 30 minutes. A site that has never asked for a sign-in counts as signed in. Signs read: a sign-in page or another site, 401, a password field, a sign-out control, a "Sign in" button that stays, and the start page as it looked signed out and signed in at your last sign-in.
- **A sign-out is recognised and fixed with a link.** When a site sends the browser to a login page, answers 401, or shows a password field, the answer is "You're signed out of <site>. Sign in here, then ask again: <link>". The link opens a page on this computer; its button opens a sign-in window (Chrome, then Playwright's Chromium, Edge last: a signed-in Edge profile takes over Microsoft sign-ins). The person signs in and presses "I've signed in"; Seira saves the cookies, opens the site with only those in a separate hidden browser, and closes the window once that browser is signed in, otherwise the window stays open. It waits up to 10 minutes, then resumes any build that was waiting. Each sign-in's steps and the pages it went through (site and path only) are kept in `<home>/signin.jsonl`. A sign-out never withdraws a tool or starts a repair.
- **The person approves deletes, not the agent.** A client with elicitation shows Seira's own question; any other client gets a link whose page has Delete and Don't delete buttons. The same call runs once within 10 minutes of approval.
- **Every call is logged** to `<home>/audit.jsonl`: user, tool, argument hash, approval, outcome, reference. Never values.
- **A broken tool is withdrawn and repaired.** A program-breaking error while signed in marks the resource degraded, hides its tools and queues a repair through every gate; a call by name meanwhile says it is being fixed.
- **A daily check** at `CONDUIT_DAILY_CHECK` (default `07:00`, `off` to disable) runs the same sign-in check on each stored session and records the answer, which calls and builds then use; `POST /api/check` runs it now.
- **Users and keys.** `python -m conduit users add <name> [--admin]` prints a key once. From the first user on, every request needs `Authorization: Bearer <key>`, and admins get `seira_block` and `seira_unblock` to keep one user away from a site, a tool, or one record:

```bash
claude mcp add --transport http seira http://127.0.0.1:4455/mcp --header "Authorization: Bearer <key>"
```

Per-connector servers (`conduit serve`, and the `server.py` in each connector folder) are developer and verification tools: they return full internal detail, which `mcp_check` reads. Give users the gateway.

## Tests

```bash
pytest --basetemp artifacts/pytest-run
```

The `--basetemp` flag is required on Windows; the default temp directory is blocked. Markers: `integration` (real browser or MCP subprocess), `desktop`, `live_model`.

## Rules

- Journals are newest first and append only.
- Never commit `.env`, `.conduit/`, sessions, or anything under `artifacts/` that a report does not need.
- No AI attribution in commit messages.
