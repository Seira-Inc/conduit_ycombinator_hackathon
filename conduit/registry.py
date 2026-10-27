"""The index the gateway answers from, rebuilt per connector when its connector.json changes;
the file stays the source of truth because the CLI writes it from other processes."""

from __future__ import annotations

import hashlib
import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path

from .generator import Store
from .models import Manifest, load_manifest, origin
from .operation_import import verified_program

VERB_WORDS = {
    "create": {"create", "add", "new", "make", "post", "insert"},
    "read": {"read", "get", "show", "find", "fetch", "view", "list", "search", "look"},
    "update": {"update", "edit", "change", "rename", "modify", "set"},
    "delete": {"delete", "remove", "drop", "erase"},
}
_HOST_RE = re.compile(r"(?:https?://)?\b([a-z0-9-]+(?:\.[a-z0-9-]+)+)(?::\d+)?", re.I)
_WORD_RE = re.compile(r"[a-z0-9]+")


def public_name(connector_id: str, tool_name: str) -> str:
    return hashlib.sha256(connector_id.encode()).hexdigest()[:8] + "__" + tool_name


def words(text: str) -> set[str]:
    return {singular(w) for w in _WORD_RE.findall((text or "").lower())}


def singular(word: str) -> str:
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def host_label(site: str) -> str:
    """app.hubspot.com -> hubspot; vikunja.example -> vikunja; 127.0.0.1 -> 127.0.0.1."""
    host = site.split("://", 1)[-1].split(":")[0].strip("[]")
    parts = host.split(".")
    if len(parts) >= 2 and not all(p.isdigit() for p in parts):
        return parts[-2]
    return host


@dataclass
class Entry:
    name: str
    connector_id: str
    tool: str
    connector_name: str
    site: str
    resource: str
    verb: str
    label: str
    read_only: bool
    description: str
    status: str
    input_schema: dict
    output_schema: dict | None
    has_session: bool = False
    tokens: set[str] = field(default_factory=set)
    resource_tokens: set[str] = field(default_factory=set)


class Registry:
    def __init__(self, store: Store):
        self.store = store
        self._mtimes: dict[str, int] = {}
        self._manifests: dict[str, Manifest] = {}
        self._entries: dict[str, Entry] = {}
        self._withdrawn: dict[str, Entry] = {}

    def refresh(self) -> None:
        seen = set()
        for path in self.store.connectors.glob("*/connector.json"):
            connector_id = path.parent.name
            try:
                mtime = path.stat().st_mtime_ns
            except OSError:
                continue
            seen.add(connector_id)
            if self._mtimes.get(connector_id) == mtime:
                continue
            try:
                manifest = load_manifest(path)
            except Exception:
                continue
            self._mtimes[connector_id] = mtime
            self._manifests[connector_id] = manifest
            self._index(manifest)
        for connector_id in set(self._manifests) - seen:
            self._drop(connector_id)
        for entry in self._entries.values():
            entry.has_session = self._session_path(entry.connector_id).exists()

    def _session_path(self, connector_id: str) -> Path:
        return self.store.home / "auth" / (connector_id + ".session")

    def invalidate(self, connector_id: str) -> None:
        """Re-read this connector on the next refresh even if its file's mtime did not move."""
        self._mtimes.pop(connector_id, None)

    def _drop(self, connector_id: str) -> None:
        self._mtimes.pop(connector_id, None)
        self._manifests.pop(connector_id, None)
        for table in (self._entries, self._withdrawn):
            for name in [n for n, e in table.items() if e.connector_id == connector_id]:
                del table[name]

    def _index(self, manifest: Manifest) -> None:
        for table in (self._entries, self._withdrawn):
            for name in [n for n, e in table.items() if e.connector_id == manifest.id]:
                del table[name]
        if manifest.kind != "website":
            return
        try:
            site = origin(manifest.target)
        except ValueError:
            return
        labels = {c.get("resource"): c.get("label") for c in manifest.capabilities if c.get("resource")}
        statuses = {c.get("resource"): c.get("status") for c in manifest.capabilities if c.get("resource")}
        for tool in manifest.tools:
            if not verified_program(tool):
                continue
            label = labels.get(tool.resource) or tool.resource
            resource_tokens = words(tool.resource) | words(label)
            tokens = (resource_tokens | words(tool.name) | words(tool.description)
                      | words(manifest.name) | {host_label(site)})
            name = public_name(manifest.id, tool.name)
            # A degraded resource stays out of the catalog until its repair republishes it; it is
            # kept aside so a call by name can say it is being fixed.
            table = self._withdrawn if statuses.get(tool.resource) == "degraded" else self._entries
            table[name] = Entry(
                name=name, connector_id=manifest.id, tool=tool.name, connector_name=manifest.name,
                site=site, resource=tool.resource, verb=tool.operation, label=label,
                read_only=tool.read_only, description=tool.description, status=statuses.get(tool.resource) or "ready",
                input_schema=tool.input_schema, output_schema=tool.output_schema,
                tokens=tokens, resource_tokens=resource_tokens,
            )

    def entries(self) -> list[Entry]:
        return sorted(self._entries.values(), key=lambda e: e.name)

    def lookup(self, name: str) -> Entry | None:
        return self._entries.get(name)

    def withdrawn(self, name: str) -> Entry | None:
        return self._withdrawn.get(name)

    def manifest(self, connector_id: str) -> Manifest | None:
        return self._manifests.get(connector_id)

    def manifests(self) -> list[Manifest]:
        return sorted(self._manifests.values(), key=lambda m: m.id)

    def connectors_for(self, site: str) -> list[Manifest]:
        try:
            wanted = origin(site)
        except ValueError:
            return []
        found = []
        for manifest in self._manifests.values():
            if manifest.kind != "website":
                continue
            try:
                if origin(manifest.target) == wanted:
                    found.append(manifest)
            except ValueError:
                continue
        return sorted(found, key=lambda m: m.created_at, reverse=True)

    def sites(self) -> list[str]:
        return sorted({e.site for e in self._entries.values()})

    def resolve_site(self, request: str, site: str | None = None) -> tuple[str | None, str]:
        """(site origin or None, reason). The site is a hard filter: a request can never pick a
        tool on a site it did not name."""
        known = self.sites()
        if site:
            try:
                wanted = origin(site)
            except ValueError:
                return None, "unknown_site"
            if wanted in known:
                return wanted, "given"
            # app.hubspot.com and app-na2.hubspot.com are one site to the user; match the domain label
            # when it names exactly one known site.
            same = [s for s in known if host_label(s) == host_label(wanted)]
            return (same[0], "given_domain") if len(same) == 1 else (None, "unknown_site")
        for match in _HOST_RE.finditer(request):
            host = match.group(1).lower()
            hits = [s for s in known if _host_of(s) in (host, host.removeprefix("www."))]
            if hits:
                return hits[0], "url"
        request_words = words(request)
        hits = {e.site for e in self._entries.values()
                if host_label(e.site) in request_words or words(e.connector_name) <= request_words}
        if len(hits) == 1:
            return hits.pop(), "name"
        return None, ("ambiguous_site" if hits else "no_site")

    def resolve_verb(self, request: str) -> str | None:
        request_words = words(request)
        found = {verb for verb, keys in VERB_WORDS.items() if request_words & {singular(k) for k in keys}}
        return found.pop() if len(found) == 1 else None

    def find(self, request: str, site: str | None = None) -> dict:
        site_origin, site_reason = self.resolve_site(request, site)
        verb = self.resolve_verb(request)
        if site_origin is None:
            return {"status": "none", "reason": site_reason, "site": None, "verb": verb,
                    "known_sites": self.sites(), "candidates": []}
        pool = [e for e in self._entries.values() if e.site == site_origin]
        if verb is None:
            return {"status": "none", "reason": "verb_ambiguous", "site": site_origin, "verb": None,
                    "candidates": [e.name for e in pool][:3]}
        pool = [e for e in pool if e.verb == verb]
        request_words = words(request)
        scored = sorted(((len(request_words & e.resource_tokens), len(request_words & e.tokens), e) for e in pool),
                        key=lambda t: (t[0], t[1]), reverse=True)
        hits = [t for t in scored if t[0] > 0]
        resources = {e.resource for e in pool}
        if len(hits) == 1 or (len(hits) > 1 and hits[0][:2] > hits[1][:2]):
            entry, reason = hits[0][2], "resource_match"
        elif not hits and len(resources) == 1 and pool:
            entry, reason = pool[0], "single_resource"
        else:
            return {"status": "none", "reason": "resource_ambiguous" if hits else "no_resource",
                    "site": site_origin, "verb": verb, "candidates": [t[2].name for t in scored[:3]]}
        return {"status": "found", "tool": entry.name, "connector_id": entry.connector_id,
                "site": site_origin, "resource": entry.resource, "verb": verb, "read_only": entry.read_only,
                "has_session": entry.has_session, "why": reason}

    def eta_seconds(self) -> tuple[int, str]:
        durations = [m.compilation.get("duration_ms") for m in self._manifests.values()
                     if m.compilation.get("status") == "ready" and m.compilation.get("duration_ms")]
        if durations:
            return int(statistics.median(durations) / 1000), f"median of {len(durations)} builds"
        return 600, "default"


def _host_of(site: str) -> str:
    return site.split("://", 1)[-1].split(":")[0].strip("[]")
