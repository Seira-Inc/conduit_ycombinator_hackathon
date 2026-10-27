import copy

import pytest
from compiler_site import website

from conduit.compiler import compile_connector
from conduit.document_compiler import compile_document
from conduit.models import Manifest
from conduit.runtime import Runtime
from conduit.verifier import FamilyExecutor, verify_family


def recipe():
    return {"name": "shapes", "label": "Shapes", "evidence": ["s1"],
        "inspection": {"steps": [{"kind": "download", "locator": {"css": "#export"}, "name": "document"}],
                       "result": {"$ref": "vars.document"}},
        "replacement": {"steps": [{"kind": "upload", "locator": {"css": "#import"},
            "value": {"$ref": "context.after"}, "expected_document": {"$ref": "context.before"},
            "verify_program": {"steps": [
                {"kind": "download", "locator": {"css": "#export"}, "name": "document"}],
                "result": {"$ref": "vars.document"}}}]},
        "template": {"x": 10, "y": 20, "width": 80, "height": 60, "fill": "red",
                     "meta": {"label": "", "untouched": "keep"}},
        "properties": {"label": {"type": "string"}, "fill": {"type": "string"}},
        "fields": {"label": "meta.label", "fill": "fill"},
        "create_input": {"label": "Conduit {{nonce}}", "fill": "red"},
        "update_input": {"label": "Conduit edited {{nonce}}"}}


@pytest.mark.integration
@pytest.mark.parametrize("filechooser", [False, True])
async def test_document_recipe_generates_tested_mcp_without_model_execution(tmp_path, monkeypatch, filechooser):
    monkeypatch.setenv("CONDUIT_LEARNING_DISABLED", "1")
    with website() as (url, _):
        manifest = Manifest(id="recipe", name="Recipe", target=url + "/canvas", kind="website",
                            allowed_origins=[url], allow_private=True)
        runtime = Runtime(manifest, tmp_path)
        source = recipe()
        if filechooser:
            source["replacement"]["steps"][0].update(
                locator={"role": "button", "name": "Open scene"}, attribute="filechooser",
                chooser_steps=[{"role": "button", "name": "Replace scene"}])
        replies = iter([{"action": "document", "recipe": source}, {"action": "done"}])
        async def model(*args, **kwargs):
            return next(replies), {"model_calls": 1}
        try:
            result = await compile_connector(runtime, model=model, research=False)
            assert result["status"] == "ready", result
            assert result["mcp_test"]["status"] == "passed"
            assert result["mcp_test"]["restarts"] == 2
            family = next(t.config["family"] for t in manifest.tools if t.adapter == "program")
            await runtime.acquire_browser()
            executor = FamilyExecutor(runtime.browser, manifest, family)
            made = await executor.call("create", {"label": "Conduit held-out", "fill": "green"})
            await executor.call("update", {"id": made["id"], "label": "Conduit changed"})
            document = await runtime.browser.page.evaluate("JSON.parse(localStorage.getItem('drawing'))")
            actual = document["elements"][0]
            assert actual["meta"] == {"label": "Conduit changed", "untouched": "keep"}
            assert actual["fill"] == "green"
            await executor.call("delete", {"id": made["id"]})
            if filechooser:
                assert await runtime.browser.page.evaluate("importClicks") == ["open", "open", "confirm", "open", "confirm"]
        finally:
            await runtime.close()


def test_document_recipe_rejects_unguarded_replacement_and_identity_edits():
    value = recipe()
    value["replacement"]["steps"][0]["expected_document"] = None
    with pytest.raises(ValueError, match="guarded upload"):
        compile_document(value)
    value = recipe()
    value["fields"]["label"] = "id"
    with pytest.raises(ValueError, match="identity"):
        compile_document(value)
    source = recipe()
    previous = copy.deepcopy(source)
    compile_document(source)
    assert source == previous


@pytest.mark.integration
async def test_failed_document_navigation_is_reconciled_by_fresh_full_export(tmp_path):
    with website() as (url, _):
        manifest = Manifest(id="no-effect", name="No effect", target=url + "/canvas", kind="website",
                            allowed_origins=[url], allow_private=True)
        runtime = Runtime(manifest, tmp_path)
        source = recipe()
        source["replacement"]["steps"][:0] = [
            {"kind": "press", "locator": {"css": "body"}, "value": "Escape"},
            {"kind": "assert", "value": False}]
        try:
            await runtime.acquire_browser()
            report = await verify_family(runtime.browser, manifest, compile_document(source))
            assert report["status"] == "incomplete"
            assert report["creation_no_effect_confirmed"] is True
            assert report["cleanup"] == []
            assert await runtime.browser.page.locator("svg rect").count() == 0
        finally:
            await runtime.close()
