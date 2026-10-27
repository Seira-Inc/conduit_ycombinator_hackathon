import json

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.memory import create_connected_server_and_client_session
from test_compiled_mcp import dashboard
from test_gateway import READ, receipt
from test_registry import verified_connector

from conduit.generator import Store
from conduit.registry import public_name
from conduit.users import Users, hidden, refused

DELETE = public_name("demo-1", "records_delete")


def test_users_file_keys_roles_and_blocks(tmp_path):
    users = Users(tmp_path / "users.json")
    assert not users.enabled
    key = users.add("holy", admin=True)
    member = users.add("angel")
    assert key.startswith("sk_") and users.enabled and users.by_key(key)["role"] == "admin"
    assert users.by_key("sk_wrong") is None and users.by_key(None) is None
    with pytest.raises(ValueError, match="already exists"):
        users.add("angel")
    users.block("angel", tool=DELETE)
    users.block("angel", site="https://other.example")
    users.block("angel", tool=READ, record="r7")
    angel = users.get("angel")
    assert hidden(angel, site="https://example.com", tool=DELETE)
    assert hidden(angel, site="https://other.example", tool="x__anything")
    assert not hidden(angel, site="https://example.com", tool=READ)
    assert refused(angel, site="https://example.com", tool=READ, record="r7")
    assert not refused(angel, site="https://example.com", tool=READ, record="r8")
    users.unblock("angel", tool=DELETE)
    assert not hidden(users.get("angel"), site="https://example.com", tool=DELETE)
    assert "key" not in json.dumps(users.all()) and len(users.all()) == 2
    again = Users(tmp_path / "users.json")
    assert again.by_key(member)["name"] == "angel" and len(again.get("angel")["blocks"]) == 2


async def test_keys_gate_the_endpoint_and_blocks_shape_each_users_catalog(tmp_path, monkeypatch):
    verified_connector(Store(tmp_path), "demo-1", "Demo", "https://example.com", "records")
    users = Users(tmp_path / "users.json")
    admin_key, member_key = users.add("holy", admin=True), users.add("angel")

    async def fake_call(self, name, arguments, progress=None):
        return receipt(name, arguments)

    monkeypatch.setattr("conduit.runtime.Runtime.call", fake_call)

    def client(key=None):
        return httpx.AsyncClient(headers={"Authorization": "Bearer " + key} if key else {})

    async with dashboard(tmp_path) as url:
        async with httpx.AsyncClient() as plain:
            refusedresponse = await plain.post(url + "/mcp", json={}, headers={"Accept": "application/json, text/event-stream"})
        assert refusedresponse.status_code == 401 and refusedresponse.headers["www-authenticate"] == "Bearer"
        async with streamable_http_client(url + "/mcp", http_client=client(admin_key)) as (read, write, _):
            async with ClientSession(read, write) as admin:
                await admin.initialize()
                admin_tools = {t.name for t in (await admin.list_tools()).tools}
                blocked = (await admin.call_tool("seira_block", {"user": "angel", "tool": DELETE})).structuredContent
                limited = (await admin.call_tool("seira_block", {"user": "angel", "tool": READ, "record": "r7"})).structuredContent
        async with streamable_http_client(url + "/mcp", http_client=client(member_key)) as (read, write, _):
            async with ClientSession(read, write) as member:
                await member.initialize()
                member_tools = {t.name for t in (await member.list_tools()).tools}
                denied = await member.call_tool("seira_block", {"user": "holy", "tool": READ})
                refused_record = await member.call_tool(READ, {"id": "r7"})
                allowed = await member.call_tool(READ, {"id": "r8"})
    assert {"seira_block", "seira_unblock", DELETE} <= admin_tools
    assert blocked["blocks"] == [{"tool": DELETE}] and len(limited["blocks"]) == 2
    assert DELETE not in member_tools and READ in member_tools and "seira_block" not in member_tools
    assert denied.isError and denied.content[0].text == "Only an admin can do that."
    assert refused_record.isError and refused_record.content[0].text == "An admin has blocked this for you."
    assert not allowed.isError
    rows = Store(tmp_path).home.joinpath("audit.jsonl").read_text().splitlines()
    assert [json.loads(r)["user"] for r in rows] == ["holy", "holy", "angel", "angel"]


async def test_seira_find_asks_the_checker_only_when_the_match_is_undecided(app, monkeypatch):
    verified_connector(app.state.store, "hub-2", "HubSpot Companies", "https://example.com", "companies")
    asked = []

    async def fake_choose(prompt, home):
        asked.append(json.loads(prompt))
        return {"tool": public_name("hub-2", "companies_read"), "why": "a firm is a company"}, {}

    monkeypatch.setattr("conduit.checker.ask_model", fake_choose)
    monkeypatch.setattr("conduit.app.model_settings", lambda: {"available": True})
    async with create_connected_server_and_client_session(app.state.gateway) as session:
        direct = (await session.call_tool("seira_find", {"request": "read a record on example.com"})).structuredContent
        assert direct["status"] == "found" and direct["tool"] == READ and asked == []
        chosen = (await session.call_tool("seira_find", {"request": "look up the firm Acme on example.com"})).structuredContent
    assert chosen == {"status": "found", "tool": public_name("hub-2", "companies_read"), "site": "example.com",
                      "action": "read", "what": "companies"}
    assert len(asked) == 1
    assert {t["tool"] for t in asked[0]["tools"]} >= {READ, public_name("hub-2", "companies_read")}
