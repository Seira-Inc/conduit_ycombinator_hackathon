import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from compiler_site import website
from javascript_fixture import javascript_canvas, javascript_family
from test_compiler import login

from conduit.compiler import compile_connector
from conduit.models import Manifest
from conduit.programs import Program, ProgramError, ProgramRunner
from conduit.runtime import Runtime


def isolated_runner():
    return ProgramRunner(SimpleNamespace(start=AsyncMock()), None)


async def test_javascript_computes_without_host_ambient_access_or_truncation():
    program = Program(javascript='''
      const id=await api.uuid();
      const rows=Array.from({length:90},(_,i)=>({value:i*i}));
      return {id,rows,text:input.text,hidden:[typeof process,typeof require,typeof fetch,typeof document]};
    ''')
    result, metrics = await isolated_runner().run(program, {"text": "λ" * 16000})
    assert result["hidden"] == ["undefined"] * 4
    assert len(result["rows"]) == 90 and result["rows"][-1]["value"] == 89**2
    assert result["text"] == "λ" * 16000
    assert len(result["id"]) == 36
    assert metrics["model_calls"] == metrics["screenshots"] == 0


@pytest.mark.parametrize("code", ['while(true) {}', 'await api.sleep(0); while(true) {}'])
async def test_runaway_javascript_worker_is_terminated(code):
    start = time.monotonic()
    with pytest.raises(ProgramError) as error:
        await isolated_runner().run(Program(javascript=code, timeout_seconds=1), {})
    assert error.value.code == "timeout" and not error.value.uncertain
    assert time.monotonic() - start < 6
    result, _ = await isolated_runner().run(Program(javascript='return 42;'), {})
    assert result == 42


async def test_read_adapter_requires_real_observation_and_refuses_mutation():
    with pytest.raises(ProgramError, match="observe actual"):
        await isolated_runner().run(Program(javascript='return input;'), {"id": "fake"}, read_only=True)
    runner = isolated_runner()
    runner.request = AsyncMock()
    with pytest.raises(ProgramError, match="mutation"):
        await runner.run(Program(javascript='await api.request({url:"https://example.com",method:"POST"});'), {}, read_only=True)
    runner.request.assert_not_called()


async def test_caught_host_failure_cannot_retry_or_report_success():
    runner = isolated_runner()
    async def lost(step):
        runner.mutated = True
        raise ProgramError("outcome_unknown", "Lost mutation response", uncertain=True)
    runner.request = AsyncMock(side_effect=lost)
    program = Program(javascript='''
      for(let i=0;i<2;i++) {
        try {await api.request({url:"https://example.com/items",method:"POST"});}catch(e){}
      }
      return {success:true};
    ''')
    with pytest.raises(ProgramError) as error:
        await runner.run(program, {})
    assert error.value.code == "outcome_unknown" and error.value.uncertain
    assert runner.request.await_count == 1


async def test_javascript_failure_carries_actual_storage_type_without_values():
    runner = isolated_runner()
    runner.resolve = AsyncMock(return_value=None)
    program = Program(javascript='const data=await api.storage("drawing"); if (!Array.isArray(data)) throw Error("Not saved yet");')
    with pytest.raises(ProgramError) as error:
        await runner.run(program, {}, read_only=True)
    assert error.value.observations == [{"operation": "storage_read", "key": "drawing", "result_type": "null"}]


@pytest.mark.integration
@pytest.mark.parametrize("canvas", [False, True])
async def test_javascript_compilation_gate_and_actual_mcp(tmp_path, monkeypatch, canvas):
    monkeypatch.setenv("CONDUIT_LEARNING_DISABLED", "1")
    with website(True) as (url, state):
        manifest = Manifest(id="js-acceptance", name="JavaScript acceptance", target=url + ("/canvas" if canvas else ""),
                            kind="website", allowed_origins=[url], allow_private=True)
        runtime = Runtime(manifest, tmp_path)
        family = javascript_canvas() if canvas else javascript_family(state["base"], state["field"])
        answers = iter([{"action": "propose", "family": family.model_dump()}, {"action": "done"}])
        async def model(prompt, *args, **kwargs):
            assert "NORMAL JAVASCRIPT" in kwargs.get("system", "") + prompt
            return next(answers), {"model_calls": 1}
        try:
            if not canvas:
                await login(runtime)
            report = await compile_connector(runtime, model=model, research=False, program_format="javascript")
            assert report["status"] == "ready", json.dumps(report, indent=2)
            assert report["mcp_test"]["status"] == "passed"
            assert report["mcp_test"]["restarts"] == 2
            assert all(c["metrics"]["model_calls"] == c["metrics"]["screenshots"] == 0
                       for c in report["mcp_test"]["calls"])
            if canvas:
                await runtime.acquire_browser()
                await runtime.browser.start()
                assert await runtime.browser.page.evaluate("JSON.parse(localStorage.getItem('drawing')).elements") == []
            else:
                assert state["items"]["tester"] == {}
        finally:
            await runtime.close()
