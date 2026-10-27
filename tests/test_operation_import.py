import copy
import json
import os
import sys

import pytest
from compiler_site import website
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from test_compiled_mcp import dashboard
from test_compiler import family_for, login

from conduit.generator import Store
from conduit.models import Manifest
from conduit.operation_import import import_operation, operation_tool, verified_program
from conduit.programs import fingerprint
from conduit.runtime import Runtime
from conduit.verifier import FamilyExecutor


def definition(family):
    return {"name": "rename_record", "description": "Rename a record", "family": family.model_dump(),
        "fingerprint": fingerprint(family), "input_schema": family.operations["update"].input_schema,
        "verification": {"status": "passed", "scope": "single_operation", "mcp_calls": 2,
                         "restarted_execution": True, "native_state_observer": True, "learning_disabled": True}}


@pytest.mark.parametrize("change", ["pending", "fingerprint", "schema", "observer"])
def test_import_rejects_incomplete_or_changed_evidence(change):
    data = copy.deepcopy(definition(family_for("https://example.com/records")))
    if change == "pending":
        data["verification"]["status"] = "pending"
    elif change == "fingerprint":
        data["fingerprint"] = "changed"
    elif change == "schema":
        data["input_schema"] = {"type": "object"}
    else:
        data["verification"]["native_state_observer"] = False
    with pytest.raises(ValueError):
        operation_tool(data)


def test_single_operation_cannot_grant_other_verbs_or_full_coverage():
    tool = operation_tool(definition(family_for("https://example.com/records")))
    assert verified_program(tool)
    assert not verified_program(tool.model_copy(update={"operation": "delete"}))
    tool.verification["status"] = "ready"
    assert not verified_program(tool)


@pytest.mark.integration
async def test_imported_operation_portable_and_universal_mcp(tmp_path):
    with website() as (url, state):
        source_home, home = tmp_path / "source", tmp_path / "installed"
        manifest = Manifest(id="source", name="Source", target=url, kind="website",
                            allowed_origins=[url], allow_private=True)
        runtime = Runtime(manifest, source_home)
        family = family_for(state["base"])
        try:
            await login(runtime)
            executor = FamilyExecutor(runtime.browser, manifest, family.model_dump())
            item = await executor.call("create", {"title": "Before", "note": "Keep"})
            control = await executor.call("create", {"title": "Unrelated", "note": "Untouched"})
        finally:
            await runtime.close()
        directory = Store(source_home).write(manifest)
        (directory / "operation.json").write_text(json.dumps(definition(family)), encoding="utf-8")
        imported, exported = import_operation(directory, Store(home), session_home=source_home)
        assert imported.capabilities[0]["status"] == "partial"
        assert imported.capabilities[0]["operations"] == {"create": False, "read": False, "update": True, "delete": False}
        assert [tool.name for tool in imported.tools] == ["rename_record"]
        saved_auth = (home / "auth" / (imported.id + ".session")).read_bytes()
        with pytest.raises(ValueError, match="already imported"):
            import_operation(directory, Store(home), session_home=source_home)
        assert (home / "auth" / (imported.id + ".session")).read_bytes() == saved_auth
        env = {**os.environ, "CONDUIT_HOME": str(home), "CONDUIT_LEARNING_DISABLED": "1"}

        async def exercise(params, title, hub=False):
            async with stdio_client(params) as (reader, writer):
                async with ClientSession(reader, writer) as client:
                    await client.initialize()
                    # The hub forwards the gateway's listing, which adds its own seira_* tools.
                    tools = [t for t in (await client.list_tools()).tools if not t.name.startswith("seira_")]
                    assert len(tools) == 1 and tools[0].name.endswith("rename_record")
                    tool = tools[0]
                    assert tool.outputSchema
                    assert "expected_state_token" in tool.inputSchema["properties"]
                    first = await client.call_tool(tool.name, {"id": item["id"], "title": title})
                    assert not first.isError, first.content
                    receipt = first.structuredContent
                    if hub:
                        assert "metrics" not in receipt
                    else:
                        assert receipt["metrics"]["model_calls"] == receipt["metrics"]["screenshots"] == 0
                    assert receipt["data"] == state["items"]["tester"][item["id"]]
                    assert receipt["data"]["title"] == title and receipt["data"]["note"] == "Keep"
                    second = await client.call_tool(tool.name, {"id": item["id"], "title": title + " 2",
                                                              "expected_state_token": receipt["state_token"]})
                    assert not second.isError, second.content
                    before = state["writes"]
                    stale = await client.call_tool(tool.name, {"id": item["id"], "title": "Must not write",
                                                             "expected_state_token": receipt["state_token"]})
                    assert stale.isError and state["writes"] == before
                    assert state["items"]["tester"][control["id"]] == control["data"]
                    coverage = await client.read_resource("conduit://capabilities")
                    if hub:
                        assert [row["actions"] for row in json.loads(coverage.contents[0].text)] == [["update"]]
                    else:
                        assert '"partial"' in coverage.contents[0].text
        try:
            await exercise(StdioServerParameters(command=sys.executable,
                args=[str(exported / "server.py")], cwd=str(exported), env=env), "Portable Ω")
            async with dashboard(home) as dashboard_url:
                await exercise(StdioServerParameters(command=sys.executable,
                    args=["-m", "conduit", "--home", str(home), "hub", "--url", dashboard_url], env=env), "Universal Ω", hub=True)
        finally:
            # The fixture rotates CSRF during the imported connector's mutations.
            # Cleanup must use its latest saved session, not the pre-import copy.
            cleanup = Runtime(imported, home)
            try:
                for result in (item, control):
                    await cleanup.acquire_browser()
                    await FamilyExecutor(cleanup.browser, imported, family.model_dump()).call(
                        "delete", {"id": result["id"]})
                assert not state["items"]["tester"]
            finally:
                await cleanup.close()
