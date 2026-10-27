import json

import pytest
from compiler_site import website

from conduit.models import Manifest
from conduit.probes import probe_document, recover_probe
from conduit.programs import Program, ProgramError, ProgramRunner
from conduit.runtime import Runtime
from conduit.secrets import load_session


@pytest.mark.integration
@pytest.mark.parametrize("javascript,delay", [(False, 0), (True, 1200)])
async def test_draw_probe_restores_document_with_multistep_guard(tmp_path, javascript, delay):
    with website(canvas_delay_ms=delay) as (url, _):
        manifest = Manifest(id="probe", name="Probe", target=url + "/canvas", kind="website",
                            allowed_origins=[url], allow_private=True, file_access="downloads")
        runtime = Runtime(manifest, tmp_path)
        try:
            await runtime.acquire_browser()
            await runtime.browser.start()
            original = {"elements": [{"id": "existing", "x": 500, "y": 20, "width": 40,
                                       "height": 30, "fill": "green", "label": "Keep me"}]}
            await runtime.browser.page.evaluate("value => localStorage.setItem('drawing',value)", json.dumps(original))
            await runtime.browser.page.reload()
            inspection = {"steps": [{"kind": "read", "locator": {"css": "#canvas"}, "attribute": "count"},
                                    {"kind": "download", "locator": {"css": "#export"}, "name": "document"}],
                          "result": {"$ref": "vars.document"}}
            persistence = None
            if javascript:
                inspection = {"javascript": 'return await api.download({css:"#export"});'}
                persistence = {"javascript": '''
                  for(let n=0;n<10;n++) {
                    if(JSON.stringify(await api.storage('drawing'))===JSON.stringify(context.after)) return true;
                    await api.sleep(200);
                  }
                  return false;
                '''}
            box = await runtime.browser.page.locator("#canvas").bounding_box()
            steps = [{"kind": "draw", "points": [{"x": box["x"] + 20, "y": box["y"] + 20},
                                                   {"x": box["x"] + 100, "y": box["y"] + 100}]}]
            restore = {"steps": [{"kind": "upload", "locator": {"css": "#import"},
                "value": {"$ref": "context.before"}, "expected_document": {"$ref": "context.after"},
                "verify_program": inspection}]}
            result = await probe_document(runtime.browser, manifest, inspection, steps, restore, persistence)
            assert result["restored"] and result["before"] == original
            assert len(result["after"]["elements"]) == 2
            assert result["after"]["elements"][0] == original["elements"][0]
            await runtime.browser.page.reload()
            observed, _ = await ProgramRunner(runtime.browser, manifest).run(Program.model_validate(inspection), {})
            assert observed == original
            assert not list((tmp_path / "recovery").glob("*.session"))
        finally:
            await runtime.close()


@pytest.mark.integration
@pytest.mark.parametrize("concurrent", [False, True])
async def test_failed_restore_can_be_repaired_without_redrawing(tmp_path, concurrent):
    with website(canvas_delay_ms=1200) as (url, _):
        manifest = Manifest(id="restore", name="Restore", target=url + "/canvas", kind="website",
                            allowed_origins=[url], allow_private=True)
        runtime = Runtime(manifest, tmp_path)
        try:
            await runtime.acquire_browser()
            await runtime.browser.start()
            inspection = {"steps": [{"kind": "download", "locator": {"css": "#export"}, "name": "doc"}],
                          "result": {"$ref": "vars.doc"}}
            box = await runtime.browser.page.locator("#canvas").bounding_box()
            steps = [{"kind": "draw", "points": [{"x": box["x"] + 20, "y": box["y"] + 20},
                                                   {"x": box["x"] + 80, "y": box["y"] + 80}]}]
            restore = {"timeout_seconds": 1, "steps": [{"kind": "upload", "locator": {"css": "#unobserved-picker"},
                "value": {"$ref": "context.before"}, "expected_document": {"$ref": "context.after"},
                "verify_program": inspection}]}
            persistence = {"steps": [{"kind": "poll", "attempts": 10, "interval_ms": 200,
                "steps": [{"kind": "set", "name": "saved", "value": {"$storage": {"key": "drawing"}}}],
                "value": {"$equal": [{"$ref": "vars.saved"}, {"$ref": "context.after"}]}}]}
            with pytest.raises(ProgramError, match="could not confirm restoration") as failed:
                await probe_document(runtime.browser, manifest, inspection, steps, restore, persistence)
            recovery = failed.value.recovery
            assert recovery
            if concurrent:
                await runtime.browser.page.evaluate("""() => {
                    scene.elements.push({id:'someone-else',x:300,y:300,width:50,height:50,fill:'green'});persist();render();
                }""")
            restore["timeout_seconds"] = 30
            restore["steps"][0]["locator"] = {"css": "#import"}
            if concurrent:
                with pytest.raises(ProgramError, match="changed after the probe"):
                    await recover_probe(runtime.browser, manifest, recovery, inspection, restore)
                assert await runtime.browser.page.locator("svg rect").count() == 2
            else:
                result = await recover_probe(runtime.browser, manifest, recovery, inspection, restore)
                assert result["restored"] and len(result["after"]["elements"]) == 1
                assert await runtime.browser.page.locator("svg rect").count() == 0
                from pathlib import Path
                assert load_session(Path(recovery))["status"] == "restored"
        finally:
            await runtime.close()
