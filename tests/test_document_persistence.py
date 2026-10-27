import pytest
from compiler_site import website
from test_compiler import canvas_family, family_for, login

from conduit.models import Manifest
from conduit.programs import Program, ProgramError
from conduit.runtime import Runtime
from conduit.verifier import FamilyExecutor


@pytest.mark.integration
async def test_visible_canvas_edit_needs_durable_save_before_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUIT_LEARNING_DISABLED", "1")
    with website(canvas_delay_ms=1200) as (url, _):
        manifest = Manifest(id="durable", name="Durable", target=url + "/canvas", kind="website",
                            allowed_origins=[url], allow_private=True)
        runtime = Runtime(manifest, tmp_path)
        family = canvas_family()
        try:
            await runtime.acquire_browser()
            executor = FamilyExecutor(runtime.browser, manifest, family)
            made = await executor.call("create", {**family.create_input, "label": "Conduit unsaved"})
            await runtime.close()
            await runtime.acquire_browser()
            assert await executor.read(made["id"]) is None  # In-memory readback alone falsely looked successful.
            family.persistence = Program(steps=[{"kind": "poll", "attempts": 10, "interval_ms": 200,
                "steps": [{"kind": "set", "name": "stored", "value": {"$storage": {"key": "drawing"}}}],
                "value": {"$equal": [{"$find": {"items": {"$ref": "vars.stored.elements", "default": []},
                    "path": "id", "equals": {"$ref": "context.id"}}}, {"$ref": "context.after"}]}}])
            executor = FamilyExecutor(runtime.browser, manifest, family)
            made = await executor.call("create", {**family.create_input, "label": "Conduit saved"})
            assert made["metrics"]["model_calls"] == 0
            await runtime.close()
            await runtime.acquire_browser()
            assert (await executor.read(made["id"]))["label"] == "Conduit saved"
            await executor.call("update", {"id": made["id"], "fill": "blue", "label": "Conduit edited"})
            await runtime.close()
            await runtime.acquire_browser()
            assert (await executor.read(made["id"]))["label"] == "Conduit edited"
            await executor.call("delete", {"id": made["id"]})
            await runtime.close()
            await runtime.acquire_browser()
            assert await executor.read(made["id"]) is None
        finally:
            await runtime.close()


@pytest.mark.integration
@pytest.mark.parametrize("failure", ["durability", "false_steps", "false_javascript", "session_file"])
async def test_postwrite_failure_preserves_identity_and_prevents_retry(tmp_path, monkeypatch, failure):
    with website() as (url, state):
        manifest = Manifest(id="save-failure", name="Save failure", target=url, kind="website",
                            allowed_origins=[url], allow_private=True)
        runtime = Runtime(manifest, tmp_path)
        try:
            await login(runtime)
            family = family_for(state["base"])
            if failure == "durability":
                family.persistence = Program(steps=[{"kind": "assert", "value": False}])
            elif failure == "false_steps":
                family.persistence = Program(result=False)
            elif failure == "false_javascript":
                family.persistence = Program(javascript='await api.storage("drawing"); return false;')
            else:
                async def unavailable():
                    raise OSError("Simulated local session storage failure")
                monkeypatch.setattr(runtime.browser, "save", unavailable)
            executor = FamilyExecutor(runtime.browser, manifest, family)
            with pytest.raises(ProgramError) as error:
                await executor.call("create", {"title": "Conduit save failure", "note": "Keep"})
            assert error.value.uncertain is True
            assert error.value.object_id in state["items"]["tester"]
            assert state["writes"] == 1
            if failure.startswith("false_"):
                assert error.value.code == "persistence_failed"
        finally:
            await runtime.close()
