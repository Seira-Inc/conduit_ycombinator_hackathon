import json

import pytest
from compiler_site import website

from conduit.compiler import compile_connector, publish
from conduit.compiler_model import InferenceFailure
from conduit.models import Manifest, schema
from conduit.programs import Family, Program, ProgramError, ProgramRunner
from conduit.runtime import Runtime
from conduit.verifier import FamilyExecutor, verify_family


def ref(path):
    return {"$ref": path}


def family_for(base, field="title"):
    fields = {field: {"type": "string"}, "note": {"type": "string"}}
    item = {"$concat": [base + "/", {"$encode": ref("input.id")}]}
    def operation(method, url, result, props, required, body=None, accept=None):
        step = {"kind": "request", "name": "r", "method": method, "url": url}
        if method not in ("GET", "HEAD"):
            step["headers"] = {"x-csrf-token": {"$session": {"kind": "cookie", "key": "csrf"}}}
            step["body"] = body
        if accept:
            step["accept"] = accept
        return {"input_schema": schema(props, required), "program": {"steps": [step], "result": result}}
    return Family.model_validate({"name": "records", "label": "records", "mode": "direct", "evidence": ["s1"],
        "fields": {field: field, "note": "note"}, "revision_path": "version",
        "create_input": {field: "Conduit {{nonce}}", "note": "Keep this note"},
        "update_input": {field: "Conduit edited {{nonce}}"}, "operations": {
            "create": operation("POST", base, ref("vars.r.body.item"), fields, [field], ref("input")),
            "read": operation("GET", item, ref("vars.r.body.item"), {"id": {"type": "string"}}, ["id"], accept=[200, 404]),
            "update": operation("PATCH", item, ref("vars.r.body.item"), {"id": {"type": "string"}, **fields,
                "expected_revision": {"type": "integer"}}, ["id", field], {field: ref("input." + field)}),
            "delete": operation("DELETE", item, None, {"id": {"type": "string"}}, ["id"]),
        }})


async def login(runtime, account="tester"):
    await runtime.acquire_browser()
    await runtime.browser.start()
    await runtime.browser.page.get_by_label("Account").fill(account)
    await runtime.browser.page.get_by_role("button", name="Sign in").click()
    await runtime.browser.page.get_by_role("heading", name="My records").wait_for()
    await runtime.browser.save()


@pytest.mark.integration
@pytest.mark.parametrize("variant", [False, True])
async def test_complete_crud_cookies_restart_and_independent_state(tmp_path, monkeypatch, variant):
    def no_model(*args, **kwargs):
        raise AssertionError("Compiled execution must never use a model")
    monkeypatch.setattr("conduit.agent.request_decision", no_model)
    with website(variant) as (url, state):
        manifest = Manifest(id="write-test", name="Writes", target=url, kind="website", allowed_origins=[url], allow_private=True)
        runtime = Runtime(manifest, tmp_path)
        try:
            await login(runtime)
            family = family_for(state["base"], state["field"])
            report = await verify_family(runtime.browser, manifest, family)
            assert report["status"] == "ready", json.dumps(report, indent=2)
            assert report["restart_tested"]
            assert state["writes"] == 15
            assert state["items"]["tester"] == {}
            publish(manifest, family, report)
            created = await runtime.call("records_create", {state["field"]: "Previously unseen object", "note": "Preserve"})
            key = created["id"]
            assert state["items"]["tester"][key][state["field"]] == "Previously unseen object"
            await runtime.close()
            edited = await runtime.call("records_update", {"id": key, state["field"]: "New value"})
            assert edited["data"]["note"] == "Preserve"
            assert edited["metrics"]["model_calls"] == edited["metrics"]["screenshots"] == 0
            assert state["items"]["tester"][key][state["field"]] == "New value"
            removed = await runtime.call("records_delete", {"id": key})
            assert removed["deleted"] and key not in state["items"]["tester"]
            exported = json.dumps(manifest.model_dump())
            for session in state["sessions"].values():
                assert session["csrf"] not in exported
        finally:
            await runtime.close()


@pytest.mark.integration
@pytest.mark.parametrize("javascript", [False, True])
async def test_false_success_unknown_outcome_and_expired_auth(tmp_path, javascript):
    with website() as (url, state):
        manifest = Manifest(id="failure-test", name="Failures", target=url, kind="website", allowed_origins=[url], allow_private=True)
        runtime = Runtime(manifest, tmp_path)
        try:
            await login(runtime)
            family = family_for(state["base"])
            if javascript:
                from javascript_fixture import javascript_family
                family = javascript_family(state["base"])
            executor = FamilyExecutor(runtime.browser, manifest, family)
            created = await executor.call("create", {"title": "Original", "note": "Untouched"})
            key = created["id"]
            state["false_update"] = True
            with pytest.raises(ProgramError, match="readback") as failure:
                await executor.call("update", {"id": key, "title": "Did not happen"})
            assert failure.value.object_id == key
            assert failure.value.postcondition == {"object_present": True, "identity_matches": True,
                                                   "mismatched_fields": ["title"], "unrelated_changed_fields": []}
            assert state["items"]["tester"][key]["title"] == "Original"
            state["false_update"], state["drop_update"] = False, True
            before = state["writes"]
            result = await executor.call("update", {"id": key, "title": "Lost response"})
            assert result["reconciled"] and state["writes"] == before + 1
            with pytest.raises(ProgramError, match="edited"):
                await executor.call("update", {"id": key, "title": "Stale", "expected_revision": 0})
            state["sessions"].clear()
            with pytest.raises(ProgramError, match="login"):
                await executor.call("delete", {"id": key})
            assert key in state["items"]["tester"]
        finally:
            await runtime.close()


def canvas_family():
    doc = ref("vars.document")
    selected = {"$find": {"items": ref("vars.document.elements"), "path": "id", "equals": ref("input.id")}}
    exported = {"kind": "download", "locator": {"css": "#export"}, "name": "document"}
    def mutation(action, value):
        patch = {"$patch": {"document": doc, "operation": action, "id": ref("input.id"), "value": value}}
        return [exported, {"kind": "set", "name": "patched", "value": patch},
                {"kind": "upload", "locator": {"css": "#import"}, "value": ref("vars.patched"),
                 "expected_document": doc, "export_locator": {"css": "#export"}}]
    props = {k: {"type": "number"} for k in ("x", "y", "width", "height")}
    props["fill"] = {"type": "string"}
    props["label"] = {"type": "string"}
    create_steps = [{"kind": "set", "name": "key", "value": {"$uuid": True}}, exported,
                    {"kind": "set", "name": "shape", "value": {"$merge": [ref("input"), {"id": ref("vars.key")}]}}]
    create_steps += [{"kind": "set", "name": "patched", "value": {"$patch": {"document": doc, "operation": "create", "value": ref("vars.shape")}}},
                     {"kind": "upload", "locator": {"css": "#import"}, "value": ref("vars.patched"),
                      "expected_document": doc, "export_locator": {"css": "#export"}}]
    return Family.model_validate({"name": "shapes", "label": "shapes", "mode": "document", "evidence": ["s1"],
        "fields": {k: k for k in props}, "create_input": {"x": 20, "y": 30, "width": 100, "height": 80,
            "fill": "red", "label": "Conduit {{nonce}}"}, "update_input": {"fill": "blue", "label": "Conduit edited {{nonce}}"},
        "operations": {
            "create": {"input_schema": schema(props, list(props)), "program": {"steps": create_steps, "result": ref("vars.shape")}},
            "read": {"input_schema": schema({"id": {"type": "string"}}, ["id"]), "program": {"steps": [exported], "result": selected}},
            "update": {"input_schema": schema({"id": {"type": "string"}, **props}, ["id", "fill"]),
                       "program": {"steps": mutation("update", {"fill": ref("input.fill"), "label": ref("input.label")}), "result": None}},
            "delete": {"input_schema": schema({"id": {"type": "string"}}, ["id"]), "program": {"steps": mutation("delete", None), "result": None}},
        }})


@pytest.mark.integration
async def test_canvas_document_crud_preserves_other_shapes(tmp_path):
    with website() as (url, _):
        manifest = Manifest(id="canvas-test", name="Canvas", target=url + "/canvas", kind="website", allowed_origins=[url], allow_private=True)
        runtime = Runtime(manifest, tmp_path)
        try:
            await runtime.acquire_browser()
            await runtime.browser.start()
            await runtime.browser.page.evaluate("""() => localStorage.setItem('drawing',JSON.stringify({elements:[
                {id:'existing',x:400,y:30,width:40,height:40,fill:'green',label:'Keep me'}]}))""")
            await runtime.browser.page.reload()
            report = await verify_family(runtime.browser, manifest, canvas_family())
            assert report["status"] == "ready", json.dumps(report, indent=2)
            data = await runtime.browser.page.evaluate("JSON.parse(localStorage.getItem('drawing'))")
            assert len(data["elements"]) == 1
            assert data["elements"][0]["label"] == "Keep me"
            assert await runtime.browser.page.locator('svg rect[data-id="existing"]').count() == 1
        finally:
            await runtime.close()


@pytest.mark.integration
async def test_compile_loop_certifies_and_rejects_ungrounded_program(tmp_path):
    with website(True) as (url, state):
        manifest = Manifest(id="learning", name="Learning", target=url, kind="website", allowed_origins=[url], allow_private=True)
        manifest.capabilities.append({"resource": "A supported anti-forgery token binding", "status": "incomplete",
                                      "operations": {v: False for v in ("create", "read", "update", "delete")}})
        runtime = Runtime(manifest, tmp_path)
        calls = []
        async def model(prompt, home, **kwargs):
            calls.append(prompt)
            if len(calls) == 1:
                return {"action": "propose", "family": family_for(state["base"], state["field"]).model_dump()}, {"model_calls": 1}
            return {"action": "done"}, {"model_calls": 1}
        try:
            await login(runtime)
            report = await compile_connector(runtime, model=model, research=False)
            assert report["status"] == "ready", report
            assert report["mcp_test"]["status"] == "passed"
            assert report["mcp_test"]["restarts"] == 2
            assert all(c["metrics"]["model_calls"] == 0 for c in report["mcp_test"]["calls"])
            assert {t.name for t in manifest.tools} == {"records_" + v for v in ("create", "read", "update", "delete")}
            assert state["items"]["tester"] == {}
            assert state["base"] in calls[0]
            assert "session secret" in calls[0]
            assert any(f.get("detail") == "A supported anti-forgery token binding" for f in manifest.findings)
        finally:
            await runtime.close()


@pytest.mark.integration
async def test_lost_create_stops_learning_without_retry(tmp_path):
    with website() as (url, state):
        manifest = Manifest(id="unknown", name="Unknown outcome", target=url, kind="website", allowed_origins=[url], allow_private=True)
        runtime = Runtime(manifest, tmp_path)
        calls = 0
        async def model(*args, **kwargs):
            nonlocal calls
            calls += 1
            return {"action": "propose", "family": family_for(state["base"]).model_dump()}, {"model_calls": 1}
        try:
            await login(runtime)
            state["drop_create"] = True
            report = await compile_connector(runtime, model=model, research=False)
            assert report["status"] == "incomplete"
            assert calls == 1 and state["writes"] == 1
            assert len(state["items"]["tester"]) == 1
            assert report["attempts"][0]["cleanup"][0]["outcome_unknown"]
            assert not any(t.adapter == "program" for t in manifest.tools)
            with pytest.raises(ValueError, match="unresolved"):
                await compile_connector(runtime, model=model, research=False)
            assert state["writes"] == 1
        finally:
            await runtime.close()


@pytest.mark.integration
@pytest.mark.parametrize("usage", [None, {"input_tokens": 50, "output_tokens": 1000}])
async def test_failed_inference_preserves_cause_for_retry_without_any_website_write(tmp_path, usage):
    with website() as (url, state):
        manifest = Manifest(id="inference-feedback", name="Inference feedback", target=url, kind="website",
                            allowed_origins=[url], allow_private=True)
        runtime = Runtime(manifest, tmp_path)
        calls, progress = [], []

        async def model(prompt, *args, **kwargs):
            calls.append(prompt)
            if len(calls) == 1:
                if usage:
                    raise InferenceFailure("Compiler output truncated at 1000 tokens", {"model_calls": 1, "usage": usage})
                raise ValueError("Compiler output truncated at 1000 tokens")
            assert "Compiler output truncated at 1000 tokens" in prompt
            return {"action": "incomplete", "reason": "Need another observation", "resources": ["records"]}, {"model_calls": 1}

        async def update(message):
            progress.append(message)

        try:
            await login(runtime)
            report = await compile_connector(runtime, model=model, progress=update, research=False, max_rounds=2)
            assert report["status"] == "incomplete" and state["writes"] == 0 and len(calls) == 2
            assert report["inference_errors"] == [{"message": "Compiler output truncated at 1000 tokens", "usage": usage}]
            assert report["model_calls"] == (2 if usage else 1)
            if usage:
                assert report["inference"][0]["usage"] == usage
            assert any("truncated at 1000 tokens" in message for message in progress)
        finally:
            await runtime.close()


@pytest.mark.integration
async def test_document_change_between_export_and_import_is_preserved(tmp_path):
    with website() as (url, _):
        manifest = Manifest(id="conflict", name="Conflict", target=url + "/canvas", kind="website", allowed_origins=[url], allow_private=True, file_access="downloads")
        runtime = Runtime(manifest, tmp_path)
        try:
            await runtime.acquire_browser()
            runner = ProgramRunner(runtime.browser, manifest)
            old, _ = await runner.run(Program(steps=[{"kind": "download", "locator": {"css": "#export"}, "name": "doc"}], result=ref("vars.doc")), {})
            assert not await runtime.browser.page.evaluate("'showOpenFilePicker' in window")
            await runtime.browser.page.evaluate("""() => {
                scene.elements.push({id:'concurrent',x:1,y:2,width:10,height:20,fill:'red'});
                localStorage.setItem('drawing',JSON.stringify(scene));render();
            }""")
            with pytest.raises(ProgramError, match="document changed"):
                await runner.run(Program(steps=[{"kind": "upload", "locator": {"css": "#import"},
                    "value": {"elements": []}, "expected_document": old, "export_locator": {"css": "#export"}}]), {})
            current = await runtime.browser.page.evaluate("JSON.parse(localStorage.getItem('drawing'))")
            assert current["elements"][0]["id"] == "concurrent"
        finally:
            await runtime.close()


@pytest.mark.integration
@pytest.mark.parametrize("result", [ref("vars.r.body"), ref("vars.r.body.nonexistent")])
async def test_bad_create_binding_recovers_only_for_cleanup(tmp_path, result):
    with website() as (url, state):
        manifest = Manifest(id="bad-binding", name="Bad binding", target=url, kind="website",
                            allowed_origins=[url], allow_private=True)
        runtime = Runtime(manifest, tmp_path)
        try:
            await login(runtime)
            family = family_for(state["base"])
            family.operations["create"].program.result = result
            report = await verify_family(runtime.browser, manifest, family)
            assert report["status"] == "incomplete"
            assert report["creation_identity_recovered"]
            assert report["cleanup"] == [{"status": "removed"}]
            assert state["items"]["tester"] == {}
            assert state["writes"] == 2  # One create, one verified cleanup; no repeated create.
        finally:
            await runtime.close()


@pytest.mark.integration
@pytest.mark.parametrize("existing", [False, True])
async def test_failed_mcp_check_withholds_candidate_and_preserves_previous(tmp_path, monkeypatch, existing):
    with website() as (url, state):
        manifest = Manifest(id="wire-failure", name="Wire failure", target=url, kind="website",
                            allowed_origins=[url], allow_private=True)
        runtime = Runtime(manifest, tmp_path)
        family = family_for(state["base"])
        async def failed_wire(*args, **kwargs):
            return {"status": "incomplete", "error": "Transport failed", "cleanup": []}
        monkeypatch.setattr("conduit.mcp_check.verify_export", failed_wire)
        responses = iter([{"action": "propose", "family": family.model_dump()}, {"action": "done"}])
        async def model(*args, **kwargs):
            return next(responses), {"model_calls": 1}
        try:
            await login(runtime)
            if existing:
                checked = await verify_family(runtime.browser, manifest, family)
                assert checked["status"] == "ready"
                publish(manifest, family, checked)
            report = await compile_connector(runtime, model=model, research=False)
            assert report["status"] == "incomplete"
            assert report["mcp_test"]["status"] == "incomplete"
            assert state["items"]["tester"] == {}
            if existing:
                assert all(t.adapter_version == 1 for t in manifest.tools)
                assert manifest.capabilities[0]["replacement_attempt"]["status"] == "incomplete"
                made = await runtime.call("records_create", {"title": "Preserved adapter", "note": "Still works"})
                await runtime.call("records_delete", {"id": made["id"]})
            else:
                assert not manifest.tools
                assert manifest.capabilities[0]["status"] == "incomplete"
        finally:
            await runtime.close()
