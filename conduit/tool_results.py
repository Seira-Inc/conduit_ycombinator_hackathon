"""One result shape for every surface that runs a tool; `verify_export` reads `object_id`,
`postcondition` and `execution_observations` off an error, so every surface must carry them."""

from __future__ import annotations

import json

from jsonschema import Draft202012Validator, ValidationError
from mcp import types

from .programs import ProgramError
from .session_lock import SessionBusy


def safe_message(exc: BaseException) -> str:
    # Transport exceptions can contain credential-bearing URLs. Expose only safe local errors.
    if isinstance(exc, (ValueError, RuntimeError)):
        return str(exc)[:1200]
    return f"{type(exc).__name__}: the action failed."


def error_payload(exc: BaseException) -> dict:
    if isinstance(exc, ProgramError):
        error = {"status": "error", "code": exc.code, "message": str(exc),
                 "outcome_unknown": exc.uncertain, "mutation_retried": False}
        if getattr(exc, "object_id", None) is not None:
            error["object_id"] = exc.object_id
        if hasattr(exc, "postcondition"):
            error["postcondition"] = exc.postcondition
        if getattr(exc, "observations", None):
            error["execution_observations"] = exc.observations
        return error
    code = "session_in_use" if isinstance(exc, SessionBusy) else "error"
    return {"status": "error", "code": code, "message": safe_message(exc),
            "outcome_unknown": False, "mutation_retried": False}


def error_result(exc: BaseException) -> types.CallToolResult:
    if isinstance(exc, (ProgramError, SessionBusy)):
        text = json.dumps(error_payload(exc))
    else:
        text = safe_message(exc)
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], isError=True)


def success_result(result, output_schema: dict | None = None) -> types.CallToolResult:
    content = []
    if isinstance(result, dict) and "image_base64" in result:
        result = dict(result)
        content.append(types.ImageContent(type="image", data=result.pop("image_base64"),
                                          mimeType=result.pop("mime_type", "image/png")))
    content.append(types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False)))
    structured = result if isinstance(result, dict) and output_schema else None
    if structured is not None:
        # The SDK client validates every non-error result against the declared schema and discards
        # the whole result on a mismatch, or when the structured part is missing. A receipt that
        # breaks its contract is therefore reported as an error that still carries the receipt.
        try:
            Draft202012Validator(output_schema).validate(structured)
        except ValidationError as exc:
            payload = {"status": "error", "code": "receipt_schema_mismatch",
                       "message": "Receipt did not match the tool's declared output schema: " + exc.message[:300],
                       "outcome_unknown": False, "mutation_retried": False, "receipt": result}
            content = [c for c in content if c.type == "image"]
            content.append(types.TextContent(type="text", text=json.dumps(payload, ensure_ascii=False)))
            return types.CallToolResult(content=content, isError=True)
    return types.CallToolResult(content=content, structuredContent=structured, isError=False)
