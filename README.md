# conduit

Seira's just-in-time tool engine. Point it at a website you are signed in to; it learns the site, builds tools that create, read, update and delete records there, proves them against the live site, and serves them over MCP with no model calls at run time.

![The just-in-time loop](docs/design/jit-loop.png)

## Read first

- `journal.md`: what happened and what was decided, newest first. `problems_encountered.md`: open and resolved problems, newest first. Both append-only.
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

It lists every verified tool across your connectors, plus `seira_find` (is there a tool for this?), `seira_build` (build one just in time; returns a job id, the build takes minutes) and `seira_status` (how is that job doing?). The endpoint accepts requests with no `Origin` header or a localhost one, so browser-based MCP clients are refused by design. Claude Desktop keeps using `python -m conduit hub` over stdio until the gateway is reachable from the internet.

What the endpoint enforces:

- **Deletes ask first.** A client with elicitation gets a form; any other client gets `confirmation_required` with a preview and a one-use `confirm_token` to pass back once the user agrees.
- **Every call is logged** to `<home>/audit.jsonl`: user, tool, argument hash, approval, outcome. Never values.
- **A broken tool is withdrawn and repaired.** A program-breaking error marks the resource degraded, hides its tools and queues a repair; the error carries `repair_job_id` for `seira_status`, or the `conduit repair` command when no model is configured.
- **A daily check** at `CONDUIT_DAILY_CHECK` (default `07:00`, `off` to disable) opens each stored session and records whether it still looks signed in; `POST /api/check` runs it now.
- **Users and keys.** `python -m conduit users add <name> [--admin]` prints a key once. From the first user on, every request needs `Authorization: Bearer <key>`, and admins get `seira_block` and `seira_unblock` to keep one user away from a site, a tool, or one record:

```bash
claude mcp add --transport http seira http://127.0.0.1:4455/mcp --header "Authorization: Bearer <key>"
```

## Tests

```bash
pytest --basetemp artifacts/pytest-run
```

The `--basetemp` flag is required on Windows; the default temp directory is blocked. Markers: `integration` (real browser or MCP subprocess), `desktop`, `live_model`.

## Rules

- Journals are newest first and append only.
- Never commit `.env`, `.conduit/`, sessions, or anything under `artifacts/` that a report does not need.
- No AI attribution in commit messages.
