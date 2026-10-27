import copy

import pytest

from conduit.models import Tool
from conduit.state_guard import state_token


def test_state_token_covers_nested_content_and_identity_but_not_dictionary_order():
    original = {"id": "one", "nested": {"title": "Original", "keep": True}, "modifiedDate": "fixed"}
    token = state_token("adapter", "one", original)
    assert token == state_token("adapter", "one", dict(reversed(list(original.items()))))
    changed = copy.deepcopy(original)
    changed["nested"]["title"] = "Edited"
    assert state_token("adapter", "one", changed) != token
    assert state_token("adapter", "two", original) != token
    assert state_token("other-adapter", "one", original) != token
    assert state_token("adapter", "one", None) is None


def test_legacy_tool_contract_upgrades_without_changing_its_program_or_inputs():
    inputs = {"type": "object", "properties": {"id": {"type": "string"}}, "additionalProperties": False}
    config = {"family": {"operations": {"update": {"input_schema": inputs}}}, "fingerprint": "unchanged"}
    before = copy.deepcopy(config)
    tool = Tool(name="records_update", adapter="program", operation="update", description="Update",
                input_schema=inputs, output_schema={"type": "object", "properties": {}}, config=config)
    assert "expected_state_token" in tool.input_schema["properties"]
    assert "state_token" in tool.output_schema["properties"]
    assert config == before and "expected_state_token" not in inputs["properties"]


@pytest.mark.integration
async def test_stale_content_is_rejected_when_vendor_revision_never_changes(tmp_path):
    from compiler_site import website
    from test_compiler import family_for, login

    from conduit.models import Manifest
    from conduit.programs import ProgramError
    from conduit.runtime import Runtime
    from conduit.verifier import FamilyExecutor

    with website() as (url, state):
        manifest = Manifest(id="state-guard", name="State guard", kind="website", target=url,
                            allowed_origins=[url], allow_private=True)
        runtime = Runtime(manifest, tmp_path)
        try:
            await login(runtime)
            family = family_for(state["base"])
            executor = FamilyExecutor(runtime.browser, manifest, family)
            initial = await executor.call("create", {"title": "Initial", "note": "Preserve"})
            key = initial["id"]
            state["items"]["tester"][key]["title"] = "Concurrent edit"
            assert state["items"]["tester"][key]["version"] == initial["data"]["version"]
            writes = state["writes"]
            for verb, extra in (("update", {"title": "Would overwrite"}), ("delete", {})):
                with pytest.raises(ProgramError, match="content changed") as error:
                    await executor.call(verb, {"id": key, "expected_state_token": initial["state_token"], **extra})
                assert error.value.code == "conflict" and not error.value.uncertain
                assert state["writes"] == writes
            current = await executor.call("read", {"id": key})
            await runtime.close()
            runtime = Runtime(manifest, tmp_path)
            await runtime.acquire_browser()
            executor = FamilyExecutor(runtime.browser, manifest, family)
            updated = await executor.call("update", {"id": key, "title": "Requested",
                                                    "expected_state_token": current["state_token"]})
            assert updated["data"]["note"] == "Preserve"
            deleted = await executor.call("delete", {"id": key, "expected_state_token": updated["state_token"]})
            assert deleted["state_token"] is None and not state["items"]["tester"]
        finally:
            await runtime.close()
