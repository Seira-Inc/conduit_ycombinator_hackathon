import copy
import json

import pytest
from compiler_site import website
from test_compiler import family_for, login

from conduit import optimizer
from conduit.compiler import publish
from conduit.models import Manifest
from conduit.optimizer import improvement, optimize_connector, score
from conduit.programs import Program, fingerprint
from conduit.runtime import Runtime
from conduit.verifier import FamilyExecutor, verify_family


def browser_family(base):
    family = family_for(base)
    family.mode = "browser"
    family.operations["create"].program = Program(javascript="""
        await api.navigate('/');
        await api.fill({label:'Title'},input.title);
        await api.fill({label:'Note'},input.note||'');
        await api.click({role:'button',name:'Save record'});
        for(let attempt=0;attempt<8;attempt++){
          const response=await api.request({url:BASE});
          const item=response.body.items.find(item=>item.title===input.title);
          if(item)return item;
          await api.sleep(50);
        }
        throw new Error('Created record was not found');
    """.replace("BASE", json.dumps(base)))
    return family


def without_extra_metadata(base):
    return Program(javascript="""
        const response=await api.request({url:BASE+'/'+encodeURIComponent(input.id),accept:[200,404]});
        if(response.status===404)return null;
        const {id,title,note,version}=response.body.item;
        return {id,title,note,version};
    """.replace("BASE", json.dumps(base)))


def test_selection_requires_measured_speed_or_less_work_without_material_slowdown():
    baseline = {"median_lifecycle_ms": 1000, "median_steps": 20, "median_requests": 8}
    assert improvement(baseline, {**baseline, "median_lifecycle_ms": 890}) == "at_least_10_percent_faster"
    assert improvement(baseline, {**baseline, "median_lifecycle_ms": 1000, "median_steps": 18})
    assert improvement(baseline, {**baseline, "median_lifecycle_ms": 1070, "median_steps": 18}) is None
    assert improvement(baseline, {**baseline, "median_steps": 18, "median_requests": 10}) is None
    with pytest.raises(ValueError):
        score({"status": "ready", "cases": [{"update": {}}]})


@pytest.mark.integration
@pytest.mark.parametrize("drop_metadata", [False, True])
async def test_optimizer_selects_faster_route_only_when_full_records_and_mcp_pass(tmp_path, drop_metadata):
    with website() as (url, state):
        state["extra_fields"] = {"server_only": {"owner": "Fixture workspace"}}
        manifest = Manifest(id="optimize-records", name="Optimize records", target=url, kind="website",
                            allowed_origins=[url], allow_private=True)
        runtime = Runtime(manifest, tmp_path)
        baseline = browser_family(state["base"])
        native = family_for(state["base"])
        proposed = {"create": native.operations["create"].program.model_dump()}
        if drop_metadata:
            proposed["read"] = without_extra_metadata(state["base"]).model_dump()

        async def model(prompt, *_args, **_kwargs):
            assert "CURRENT FAMILY" in prompt and "MEASUREMENTS" in prompt
            return {"action": "revise", "programs": proposed, "mode": "direct", "evidence": ["s1"]}, {"model_calls": 1}

        try:
            await login(runtime)
            checked = await verify_family(runtime.browser, manifest, baseline)
            assert checked["status"] == "ready", checked
            publish(manifest, baseline, checked)
            before = copy.deepcopy([t.model_dump() for t in manifest.tools])
            result = await optimize_connector(runtime, "records", model=model, max_rounds=1)
            assert not state["items"]["tester"], result
            candidate = result["candidates"][0]
            if drop_metadata:
                assert result["status"] == "unchanged", result
                assert candidate["verification"]["error_code"] == "independent_state_mismatch"
                assert candidate["verification"]["postcondition"]["missing_fields"] == ["server_only"]
                assert [t.model_dump() for t in manifest.tools] == before
            else:
                assert result["status"] == "selected", result
                assert candidate["verification"]["reference_checks"] == 30
                assert candidate["mcp_test"]["reference_checks"] == 10
                assert candidate["mcp_test"]["restarts"] == 2
                assert result["selected_mode"] == "direct"
                assert result["selected_score"]["median_lifecycle_ms"] < result["baseline_score"]["median_lifecycle_ms"]
                assert all(t.adapter_version == 2 for t in manifest.tools)
                assert not any(t.name.startswith("__reference_") for t in manifest.tools)
                made = await runtime.call("records_create", {"title": "Conduit independent result", "note": "Keep"})
                assert made["data"] == state["items"]["tester"][made["id"]]
                await runtime.call("records_delete", {"id": made["id"]})
        finally:
            await runtime.close()


@pytest.mark.integration
async def test_failed_reference_check_in_exported_mcp_does_not_publish_candidate(tmp_path, monkeypatch):
    with website() as (url, state):
        manifest = Manifest(id="optimize-gate", name="Optimize gate", target=url, kind="website",
                            allowed_origins=[url], allow_private=True)
        runtime = Runtime(manifest, tmp_path)
        baseline = browser_family(state["base"])
        native = family_for(state["base"])
        real_verify = optimizer.verify_export

        async def drift_during_export(*args, **kwargs):
            state["extra_fields"] = {"server_only": "New native metadata"}
            return await real_verify(*args, **kwargs)

        async def model(*_args, **_kwargs):
            return {"action": "revise", "mode": "direct", "evidence": ["s1"], "programs": {
                "create": native.operations["create"].program.model_dump(),
                "read": without_extra_metadata(state["base"]).model_dump()}}, {"model_calls": 1}

        try:
            await login(runtime)
            checked = await verify_family(runtime.browser, manifest, baseline)
            assert checked["status"] == "ready", checked
            publish(manifest, baseline, checked)
            monkeypatch.setattr(optimizer, "verify_export", drift_during_export)
            result = await optimize_connector(runtime, "records", model=model, max_rounds=1)
            assert result["candidates"][0]["verification"]["status"] == "ready"
            assert result["candidates"][0]["mcp_test"]["status"] == "incomplete"
            assert result["status"] == "needs_attention", result
            assert all(t.config["fingerprint"] == fingerprint(baseline) and t.adapter_version == 1 for t in manifest.tools)
            writes = state["writes"]
            with pytest.raises(ValueError, match="outstanding"):
                await optimize_connector(runtime, "records", model=model, max_rounds=1)
            assert state["writes"] == writes
            # Fresh baseline reads identify the isolated test objects for independent fixture cleanup.
            for key in list(state["items"]["tester"]):
                await runtime.acquire_browser()
                actual = await FamilyExecutor(runtime.browser, manifest, baseline).read(key)
                assert actual["title"].startswith("Conduit ")
                await runtime.call("records_delete", {"id": key})
            assert not state["items"]["tester"]
        finally:
            await runtime.close()
