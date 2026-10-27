from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import Error as PlaywrightError

from .discovery import discover
from .generator import Store
from .models import BuildRequest


def main():
    load_dotenv(Path.cwd() / ".env", override=False)
    parser = argparse.ArgumentParser(
        prog="conduit", description="Give a website or Windows app an MCP connector. No API spec required."
    )
    parser.add_argument("--home", type=Path, default=Path(os.environ.get("CONDUIT_HOME", ".conduit")))
    commands = parser.add_subparsers(dest="command", required=True)
    ui = commands.add_parser("ui", help="Open the local dashboard")
    ui.add_argument("--port", type=int, default=4455)
    build = commands.add_parser("create", help="Create an MCP connector")
    build.add_argument("target", help="Website URL, installed app name, app ID, or full .exe path")
    build.add_argument("--desktop", action="store_true")
    build.add_argument("--name", default="")
    build.add_argument("--spec-url")
    build.add_argument("--allow-origin", action="append", default=[])
    build.add_argument("--allow-private", action="store_true")
    build.add_argument(
        "--no-browser",
        action="store_true",
        help="Skip browser discovery; runtime browser tools are still generated",
    )
    build.add_argument("--pages", type=int, default=3)
    build.add_argument("--notes", default="")
    build.add_argument("--task", action="append", default=[])
    build.add_argument("--no-compile", action="store_true", help="Discover only; do not learn and test CRUD yet")
    compiler = commands.add_parser("compile", help="Learn, test, and publish complete CRUD tools")
    compiler.add_argument("connector_id")
    compiler.add_argument("--no-research", action="store_true")
    compiler.add_argument("--rounds", type=int, default=8)
    compiler.add_argument("--program-format", choices=["auto", "javascript", "steps"], default="auto")
    compiler.add_argument("--optimize", action="store_true", help="Compare one alternative route per verified resource")
    verify = commands.add_parser("verify", help="Test an exported MCP's CRUD tools with learning disabled")
    verify.add_argument("connector_id")
    repair = commands.add_parser("repair", help="Repair a compiled resource without repeating a failed write")
    repair.add_argument("connector_id")
    repair.add_argument("resource")
    repair.add_argument("--rounds", type=int, default=8)
    repair.add_argument("--program-format", choices=["auto", "javascript", "steps"], default="auto")
    optimizer = commands.add_parser("optimize", help="Compare simpler or faster adapters against complete verified CRUD")
    optimizer.add_argument("connector_id")
    optimizer.add_argument("resource")
    optimizer.add_argument("--rounds", type=int, choices=range(1, 5), default=2)
    hub = commands.add_parser("hub", help="One MCP server for verified tools in the local dashboard")
    hub.add_argument("--url", default="http://127.0.0.1:4455")
    hub.add_argument("--key", help="A Seira user key, when the gateway has users (default: SEIRA_KEY)")
    serve = commands.add_parser("serve", help="Serve a generated connector over MCP stdio")
    serve.add_argument("manifest", type=Path)
    serve.add_argument("--read-only", action="store_true")
    serve.add_argument("--headed", action="store_true")
    commands.add_parser("list", help="List generated connectors")
    importer = commands.add_parser("import-operation", help="Import a tested single-operation MCP bundle")
    importer.add_argument("directory", type=Path)
    importer.add_argument("--session-home", type=Path,
                          help="Explicit local source state directory whose protected session should be copied")
    commands.add_parser("upgrade", help="Update existing connector runtimes while preserving their settings")
    commands.add_parser("apps", help="List installed Windows apps")
    users = commands.add_parser("users", help="People who may call the gateway, each with a key")
    users_commands = users.add_subparsers(dest="users_command", required=True)
    add_user = users_commands.add_parser("add", help="Create a user and print the key once")
    add_user.add_argument("name")
    add_user.add_argument("--admin", action="store_true", help="May block and unblock other users")
    users_commands.add_parser("list", help="List users and their blocks")
    login = commands.add_parser("login", help="Sign in in an isolated browser")
    login.add_argument("connector_id")
    login.add_argument("--logged-in-text", help="Words that only a signed-in page shows, used to verify the sign-in")
    login.add_argument("--logged-in-selector", help="CSS selector that exists only when signed in")
    login.add_argument("--channel", choices=["msedge", "chrome", "chromium"], help="Browser channel for this sign-in")
    login.add_argument("--compile", action="store_true", help="Learn and test CRUD tools right after a verified sign-in")
    login.add_argument("--wait", type=int, metavar="SECONDS",
                       help="Watch the browser until the page looks signed in instead of waiting for Enter")
    for command in (build, compiler, repair, optimizer):
        command.add_argument("--crawl4ai-python", type=Path,
                             help="Optional Python executable with Crawl4AI for local learning evidence extraction")
    args = parser.parse_args()
    if getattr(args, "crawl4ai_python", None):
        if not args.crawl4ai_python.is_file():
            parser.error("--crawl4ai-python must point to an existing Python executable")
        os.environ["CONDUIT_CRAWL4AI_PYTHON"] = str(args.crawl4ai_python.resolve())
    try:
        if args.command == "ui":
            import uvicorn

            from .app import create_app

            print(f"Conduit is at http://127.0.0.1:{args.port}", file=sys.stderr)
            print(f"MCP front door: claude mcp add --transport http seira http://127.0.0.1:{args.port}/mcp",
                  file=sys.stderr)
            uvicorn.run(create_app(args.home), host="127.0.0.1", port=args.port, log_level="warning")
        elif args.command == "serve":
            from .mcp_server import run

            run(args.manifest, home=args.home, read_only=args.read_only, headed=args.headed)
        elif args.command == "hub":
            from .hub import serve_hub
            asyncio.run(serve_hub(args.url, args.key))
        elif args.command == "import-operation":
            from .operation_import import import_operation
            manifest, directory = import_operation(args.directory, Store(args.home), session_home=args.session_home)
            print(json.dumps({"id": manifest.id, "directory": str(directory),
                              "tool": manifest.tools[0].name, "scope": "single_operation"}, indent=2))
        elif args.command == "verify":
            from .mcp_check import verify_export
            async def verify_saved():
                store = Store(args.home)
                manifest = store.get(args.connector_id)
                async def progress(message):
                    print(message, file=sys.stderr, flush=True)
                result = await verify_export(manifest, store.write(manifest), store.home, progress=progress)
                manifest.compilation["mcp_test"] = result
                store.write(manifest)
                print(json.dumps(result, indent=2))
                return result["status"] == "passed"
            if not asyncio.run(verify_saved()):
                raise SystemExit(1)
        elif args.command in ("compile", "repair", "optimize"):
            from .compiler import compile_connector
            from .runtime import Runtime
            async def compile_saved():
                store = Store(args.home)
                runtime = Runtime(store.get(args.connector_id), store.home)
                async def progress(message):
                    print(message, file=sys.stderr, flush=True)
                try:
                    if args.command == "optimize":
                        from .optimizer import optimize_connector
                        result = await optimize_connector(runtime, args.resource, progress=progress, max_rounds=args.rounds)
                    else:
                        result = await compile_connector(runtime, progress=progress,
                              max_rounds=max(1, min(args.rounds, 60)), research=not getattr(args, "no_research", True),
                              resource=getattr(args, "resource", None), program_format=getattr(args, "program_format", "auto"))
                        if getattr(args, "optimize", False) and result["status"] == "ready":
                            from .optimizer import optimize_connector
                            for name in {t.resource for t in runtime.manifest.tools if t.adapter == "program"}:
                                optimized = await optimize_connector(runtime, name, progress=progress, max_rounds=1)
                                if optimized["status"] == "needs_attention":
                                    break
                    store.write(runtime.manifest)
                    print(json.dumps(result, indent=2))
                finally:
                    await runtime.close()
            asyncio.run(compile_saved())
        elif args.command == "create":

            async def create():
                async def progress(message):
                    print(message, file=sys.stderr)

                manifest = await discover(
                    BuildRequest(
                        target=args.target,
                        kind="desktop" if args.desktop else "website",
                        name=args.name,
                        spec_url=args.spec_url,
                        max_pages=args.pages,
                        browser=not args.no_browser,
                        allow_private=args.allow_private,
                        allowed_origins=args.allow_origin,
                        notes=args.notes,
                        tasks=args.task,
                    ),
                    args.home,
                    progress,
                )
                directory = Store(args.home).write(manifest)
                if not args.desktop and not args.no_compile:
                    from .compiler import compile_connector
                    from .runtime import Runtime
                    runtime = Runtime(manifest, args.home)
                    try:
                        await compile_connector(runtime, progress=progress)
                        Store(args.home).write(manifest)
                    finally:
                        await runtime.close()
                print(
                    json.dumps(
                        {
                            "id": manifest.id,
                            "directory": str(directory),
                            "tools": len(manifest.tools),
                            "warnings": manifest.warnings,
                        },
                        indent=2,
                    )
                )

            asyncio.run(create())
        elif args.command == "apps":
            from .desktop import bridge

            print(json.dumps(asyncio.run(bridge("catalog")), indent=2))
        elif args.command == "list":
            print(
                json.dumps(
                    [{"id": m.id, "name": m.name, "tools": len(m.tools)} for m in Store(args.home).list()],
                    indent=2,
                )
            )
        elif args.command == "upgrade":
            print(json.dumps({"updated": Store(args.home).upgrade()}, indent=2))
        elif args.command == "users":
            from .users import Users

            users = Users(Path(args.home) / "users.json")
            if args.users_command == "add":
                key = users.add(args.name, admin=args.admin)
                print(f"User {args.name} added ({'admin' if args.admin else 'member'}). The key is shown once:")
                print(key)
                print("Connect Claude Code with it:")
                print(f'  claude mcp add --transport http seira http://127.0.0.1:4455/mcp --header "Authorization: Bearer {key}"')
            else:
                print(json.dumps(users.all(), indent=2))
        elif args.command == "login":
            if args.channel:
                os.environ["CONDUIT_BROWSER_CHANNEL"] = args.channel

            async def sign_in():
                from .browser import BrowserSession
                from .discovery import refresh_forms
                from .login import confirm, fingerprint, login_finding, watch_for_sign_in
                from .session_lock import SessionLease

                store = Store(args.home)
                manifest = store.get(args.connector_id)
                if manifest.kind != "website":
                    raise ValueError("Sign in directly in the installed Windows app.")
                session = BrowserSession(manifest, store.home, headed=True)
                session_lock = SessionLease(store.home / "locks" / (manifest.id + ".lock"))
                session_lock.acquire()
                session.manual_login = True
                try:
                    await session.start()
                    before = await fingerprint(session.page)
                    seen = True
                    if args.wait:
                        print(f"Sign in in the browser window. Watching for up to {args.wait} s.", flush=True)
                        seen = await watch_for_sign_in(session, before, args.wait, text=args.logged_in_text,
                                                       selector=args.logged_in_selector)
                        print("Browser shows a signed-in page; saving the session." if seen
                              else "No signed-in page was seen in time; saving whatever the browser holds.", flush=True)
                    else:
                        await asyncio.to_thread(
                            input, "Sign in in the browser, then press Enter here to save the session. "
                        )
                    login_status = await session.finish_login()
                    if login_status == "checkpoint_recovered":
                        print("Browser closed. Reopening the last encrypted checkpoint to check sign-in.")
                finally:
                    try:
                        await session.close()
                    finally:
                        session_lock.release()
                session_lock.acquire()
                session = BrowserSession(manifest, store.home)
                try:
                    await refresh_forms(manifest, session)
                    check = await confirm(session, before, text=args.logged_in_text, selector=args.logged_in_selector,
                                          seen=seen)
                    manifest.findings.append(login_finding(check))
                    store.write(manifest)
                finally:
                    try:
                        await session.close()
                    finally:
                        session_lock.release()
                if not check["verified"]:
                    raise RuntimeError("Sign-in NOT verified: " + check["reason"] + ". The session was saved; sign in again or supply a marker.")
                print("Sign-in verified: " + check["reason"])
                if manifest.login_origins:
                    print("Sign-in passed through: " + ", ".join(manifest.login_origins))
                if not args.compile:
                    print("Run `conduit compile " + manifest.id + "` to learn and test CRUD tools.")
                    return
                from .compiler import compile_connector
                from .runtime import Runtime
                runtime = Runtime(manifest, store.home)
                async def progress(message):
                    print(message, file=sys.stderr, flush=True)
                try:
                    report = await compile_connector(runtime, progress=progress)
                    store.write(manifest)
                    print("CRUD compilation: " + report["status"])
                finally:
                    await runtime.close()

            asyncio.run(sign_in())
    except (ValueError, RuntimeError, FileNotFoundError, EOFError, PlaywrightError) as exc:
        message = str(exc).splitlines()[0] if str(exc) else "No terminal input; run this command from an interactive console."
        print("Conduit: " + message, file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
