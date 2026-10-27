from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

STATIC = Path(__file__).parents[1] / "conduit" / "static"


@pytest.fixture(autouse=True)
def signed_in_unless_checked(request, monkeypatch):
    """Fast tests never open a browser to check a sign-in; integration tests and tests marked
    sign_in_check run the real check."""
    if request.node.get_closest_marker("integration") or request.node.get_closest_marker("sign_in_check"):
        return

    async def signed_in(self, **kwargs):
        return "signed_in", None

    monkeypatch.setattr("conduit.runtime.Runtime.check_sign_in", signed_in)


@pytest.fixture
def app(tmp_path):
    """A dashboard app over a temporary home holding one verified connector, demo-1 on example.com."""
    from test_registry import verified_connector

    from conduit.app import create_app

    app = create_app(tmp_path, mcp_json_response=True)
    manifest = verified_connector(app.state.store, "demo-1", "Demo", "https://example.com", "records")
    manifest.notes = "SECRET-NOTE: create the contact John Doe"
    app.state.store.write(manifest)
    return app


@pytest.fixture
def local_site():
    state = {"items": {}, "next_id": 1, "writes": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, body, status=200, content_type="application/json"):
            if not isinstance(body, bytes):
                body = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path in ("/", "/playground"):
                return self.send((STATIC / "playground.html").read_bytes(), content_type="text/html")
            if path.startswith("/static/"):
                file = STATIC / Path(path).name
                if file.is_file():
                    return self.send(
                        file.read_bytes(),
                        content_type="text/css" if file.suffix == ".css" else "application/javascript",
                    )
            if path == "/description.json":
                return self.send(
                    {
                        "openapi": "3.0.3",
                        "info": {"title": "Notes", "version": "1"},
                        "servers": [{"url": "/api"}],
                        "components": {
                            "securitySchemes": {
                                "Token": {"type": "apiKey", "in": "header", "name": "X-Token"}
                            }
                        },
                        "paths": {
                            "/items": {
                                "get": {
                                    "operationId": "list_items",
                                    "parameters": [
                                        {
                                            "name": "tag",
                                            "in": "query",
                                            "schema": {"type": "array", "items": {"type": "string"}},
                                        }
                                    ],
                                    "responses": {},
                                },
                                "post": {
                                    "operationId": "create_item",
                                    "security": [{"Token": []}],
                                    "requestBody": {
                                        "required": True,
                                        "content": {
                                            "application/json": {
                                                "schema": {
                                                    "type": "object",
                                                    "properties": {"title": {"type": "string"}},
                                                    "required": ["title"],
                                                }
                                            }
                                        },
                                    },
                                    "responses": {},
                                },
                            },
                            "/items/{id}": {
                                "parameters": [
                                    {
                                        "name": "id",
                                        "in": "path",
                                        "required": True,
                                        "schema": {"type": "string"},
                                    }
                                ],
                                "delete": {"operationId": "delete_item", "responses": {}},
                            },
                        },
                    }
                )
            if path == "/api/items":
                return self.send(
                    {"items": state["items"], "tags": parse_qs(urlsplit(self.path).query).get("tag", [])}
                )
            if path == "/login":
                return self.send(
                    b"""<!doctype html><title>Sign in</title><label>Name<input name="name" aria-label="Name"></label><button onclick="document.cookie='test_session=present;path=/';document.body.innerHTML='<h1>Signed in</h1>'">Sign in</button>""",
                    content_type="text/html",
                )
            if path == "/landing":
                # Like Microsoft Forms: a neutral loading page that later becomes the app or a sign-in button.
                return self.send(
                    b"""<!doctype html><title>Landing</title><body>Loading<script>setTimeout(() => {"""
                    b"""if (document.cookie.includes('test_session=present')) location.href = '/app';"""
                    b"""else document.body.innerHTML = '<button>Sign in</button>';}, 800);</script>""",
                    content_type="text/html",
                )
            if path == "/app":
                return self.send(b"<!doctype html><title>App</title><h1>Your forms</h1>", content_type="text/html")
            if path == "/session":
                signed_in = "test_session=present" in self.headers.get("Cookie", "")
                return self.send(
                    ("<h1>" + ("Signed in" if signed_in else "Signed out") + "</h1>").encode(),
                    content_type="text/html",
                )
            if path == "/offsite":
                self.send_response(302)
                self.send_header("Location", f"http://localhost:{self.server.server_port}/session")
                self.end_headers()
                return
            if path == "/protected":
                self.send_response(302)
                self.send_header("Location", "/signin")
                self.end_headers()
                return
            if path == "/signin":
                return self.send(
                    b"""<!doctype html><title>Sign in</title><label>Email<input type="email" name="email"></label>"""
                    b"""<label>Password<input type="password" name="password"></label><button>Sign in</button>""",
                    content_type="text/html",
                )
            if path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "http://localhost:1/outside")
                self.end_headers()
                return
            self.send({"error": "no API specification at this URL"}, 404)

        def do_POST(self):
            if self.path == "/api/items":
                if self.headers.get("X-Token") != "test-only-key":
                    return self.send({"error": "auth"}, 401)
                data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                key = str(state["next_id"])
                state["next_id"] += 1
                state["items"][key] = data["title"]
                state["writes"] += 1
                return self.send({"id": key, "title": data["title"]}, 201)
            self.send({"error": "not found"}, 404)

        def do_DELETE(self):
            key = self.path.rsplit("/", 1)[-1]
            state["items"].pop(key, None)
            state["writes"] += 1
            self.send({"deleted": key})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", state
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)
