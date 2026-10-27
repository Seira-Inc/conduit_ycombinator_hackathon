"""What a person reads when a call does not go to plan: one fixed sentence per situation, filled
only with values Seira controls. Exception text and website content never reach a client; the
internal story is kept in errors.jsonl under the reference the sentence gives."""

from __future__ import annotations

import copy
import re
from urllib.parse import urlsplit

from mcp import types

from .registry import singular

CODE_KIND = {
    "authentication_required": "sign_in_maybe",
    "object_missing": "not_found",
    "conflict": "changed_since_read",
    "ambiguous_object": "ambiguous",
    "outcome_unknown": "outcome_unknown",
    "receipt_schema_mismatch": "outcome_unknown",
    "session_persistence_failed": "outcome_unknown",
    "create_identifier_missing": "outcome_unknown",
    "session_in_use": "busy",
    "connector_busy": "busy",
    "forbidden": "blocked",
    "ungrounded_write": "unavailable",
}

TEMPLATES = {
    "sign_in_required": "You're signed out of {site}. Sign in here, then ask again: {link}",
    "sign_in_maybe": "{Site} refused this. You may need to sign in again: {link}",
    "no_access": "You don't have access to that on {site}. Ask its owner for access.",
    "not_found": "There's no {thing} with that ID on {site}.",
    "ambiguous": "More than one {thing} on {site} matches. Give its ID.",
    "changed_since_read": "That {thing} changed since you last read it. Read it again, then retry.",
    "outcome_unknown": "Seira couldn't confirm whether the {thing} was {past}{id_note}. Don't repeat it; "
                       "read it first to check. Reference {ref}.",
    "invalid_input": "{problem}",
    "blocked": "An admin has blocked this for you.",
    "busy": "Seira is busy with {site} right now. Try again in a minute.",
    "temporarily_unavailable": "{Verbing} {things} on {site} isn't working right now. Seira is fixing it; "
                               "try again in a few minutes. Reference {ref}.",
    "unavailable": "{Verbing} {things} on {site} isn't available right now. Your administrator can see why. "
                   "Reference {ref}.",
    "approval_required": "Approve deleting the {thing}{id_note} on {site} here: {link} Once it's approved, "
                         "ask me again.",
    "not_approved": "The delete wasn't approved, so nothing was changed.",
    "build_unavailable": "Seira can't build new tools right now. Your administrator can see why.",
    "unknown_tool": "That tool isn't available. Use seira_find to see what is.",
    "admin_only": "Only an admin can do that.",
    "failed": "That didn't work, and nothing was changed. Reference {ref}.",
}

NEEDS_REFERENCE = {"outcome_unknown", "temporarily_unavailable", "unavailable", "failed"}

VERBING = {"create": "creating", "read": "reading", "update": "changing", "delete": "deleting"}
PAST = {"create": "created", "read": "read", "update": "changed", "delete": "deleted"}
PHRASE = {"create": "create a {thing}", "read": "read a {thing} by its ID", "update": "change a {thing}",
          "delete": "delete a {thing}"}

CONSTRAINT = {
    "required": "The request is missing \"{field}\".",
    "type": "The \"{field}\" value has the wrong type.",
    "enum": "The \"{field}\" value isn't one of the allowed choices.",
    "const": "The \"{field}\" value isn't one of the allowed choices.",
    "additionalProperties": "The request has a field this tool doesn't take.",
    "pattern": "The \"{field}\" value isn't in the right format.",
    "format": "The \"{field}\" value isn't in the right format.",
    "minLength": "The \"{field}\" value is too short.",
    "maxLength": "The \"{field}\" value is too long.",
    "minimum": "The \"{field}\" value is too small.",
    "maximum": "The \"{field}\" value is too large.",
    "minItems": "The \"{field}\" list has too few items.",
    "maxItems": "The \"{field}\" list has too many items.",
}

SCHEMA_KEYS = {"type", "properties", "required", "items", "enum", "const", "format", "pattern", "minLength",
               "maxLength", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "minItems", "maxItems",
               "uniqueItems", "additionalProperties", "anyOf", "oneOf", "allOf", "not", "nullable"}
INTERNAL_RECEIPT = ("metrics", "adapter_version", "reconciled")


def site_label(site: str) -> str:
    host = (urlsplit(site).hostname or "") if "://" in (site or "") else (site or "")
    host = host.lower().removeprefix("www.")
    return host if re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,79}", host) else "the site"


def things(resource: str) -> str:
    words = re.sub(r"[^a-z0-9]+", " ", (resource or "").lower()).split()
    return " ".join(words[:4]) or "items"


def thing(resource: str) -> str:
    words = things(resource).split()
    return " ".join(words[:-1] + [singular(words[-1])]) if words != ["items"] else "item"


def id_note(value) -> str:
    text = "" if value is None else str(value)
    return f" (ID {text})" if re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", text) else ""


def field_name(value) -> str | None:
    text = "" if value is None else str(value)
    return text if re.fullmatch(r"[A-Za-z0-9_.\[\]-]{1,60}", text) and text != "arguments" else None


def kind_for(code: str | None, uncertain: bool = False) -> str:
    if uncertain:
        return "outcome_unknown"
    return CODE_KIND.get(code or "", "failed")


def problem_for(field, constraint) -> str:
    name = field_name(field)
    if constraint == "additionalProperties":
        return CONSTRAINT["additionalProperties"]
    if name is None:
        return "A value in the request isn't valid for this tool."
    return CONSTRAINT.get(constraint or "", "The \"{field}\" value isn't valid.").format(field=name)


def sentence(kind: str, *, site: str = "", resource: str = "", verb: str = "", ref: str | None = None,
             link: str | None = None, object_id=None, problem: str | None = None, token: str | None = None) -> str:
    label = site_label(site) if site else "the site"
    verbing = VERBING.get(verb, "using")
    slots = {
        "site": label, "Site": label[:1].upper() + label[1:], "thing": thing(resource),
        "things": things(resource), "verbing": verbing, "Verbing": verbing.capitalize(),
        "past": PAST.get(verb, "done"), "id_note": id_note(object_id), "ref": ref or "-",
        "link": link or "open Seira on this computer", "problem": problem or "The request isn't valid.",
        "token": token or "",
    }
    return TEMPLATES.get(kind, TEMPLATES["failed"]).format(**slots)


def failure(text: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], isError=True)


def describe(site: str, resource: str, verb: str) -> str:
    text = f"{site_label(site)}: {PHRASE.get(verb, 'use a {thing}').format(thing=thing(resource))}."
    if verb == "delete":
        text += " The user approves each delete."
    return text


def public_schema(schema, *, drop=()) -> dict | None:
    """Only structural keywords survive; descriptions, titles and examples were written by the
    compiler model from website content and could carry instructions."""
    if not isinstance(schema, dict):
        return schema

    def clean(node):
        if isinstance(node, list):
            return [clean(item) for item in node]
        if not isinstance(node, dict):
            return node
        out = {}
        for key, value in node.items():
            if key not in SCHEMA_KEYS:
                continue
            if key == "properties" and isinstance(value, dict):
                out[key] = {name: clean(sub) for name, sub in value.items()}
            elif key in ("items", "additionalProperties", "not") and isinstance(value, dict):
                out[key] = clean(value)
            elif key in ("anyOf", "oneOf", "allOf") and isinstance(value, list):
                out[key] = [clean(sub) for sub in value]
            else:
                out[key] = copy.deepcopy(value)
        return out

    cleaned = clean(schema)
    for name in drop:
        cleaned.get("properties", {}).pop(name, None)
    if "required" in cleaned:
        cleaned["required"] = [name for name in cleaned["required"] if name not in drop]
    return cleaned


def public_output_schema(schema) -> dict | None:
    return public_schema(schema, drop=INTERNAL_RECEIPT) if schema else None


def public_receipt(result):
    if not isinstance(result, dict):
        return result
    return {key: value for key, value in result.items() if key not in INTERNAL_RECEIPT}
