"""Append-only record of every tool call through the gateway: who, which tool, how it was approved,
how it ended. Arguments are hashed, never stored, and receipts stay out of it."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import time
from pathlib import Path

REDACTIONS = [
    (re.compile(r"[a-z][a-z0-9+.-]*://\S+", re.I), "<url>"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "<email>"),
    (re.compile(r"\d{6,}"), "<number>"),
]


def redact(text) -> str:
    text = str(text or "")
    for pattern, replacement in REDACTIONS:
        text = pattern.sub(replacement, text)
    return text[:500]


def argument_hash(arguments) -> str:
    return hashlib.sha256(json.dumps(arguments, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


class Audit:
    def __init__(self, path: Path):
        self.path = path

    def record(self, **fields) -> dict:
        row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **fields}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        return row

    def tail(self, count: int = 50) -> list[dict]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        rows = []
        for line in lines[-count:]:
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
        return rows


class ErrorLog(Audit):
    """What went wrong behind a reference the user was given: the internal code and message,
    redacted and cut short, kept for a limited time and never sent to a client."""

    def __init__(self, path: Path, *, keep_days: int = 30):
        super().__init__(path)
        self.keep_days = keep_days
        self._issued: set[str] = set()

    def new_ref(self) -> str:
        while True:
            ref = base64.b32encode(secrets.token_bytes(5)).decode("ascii")
            if ref not in self._issued:
                self._issued.add(ref)
                return ref

    def log(self, ref: str, **detail) -> dict:
        return self.record(ref=ref, **{key: redact(value) if isinstance(value, str) else value
                                       for key, value in detail.items()})

    def find(self, ref: str) -> dict | None:
        return next((row for row in reversed(self.tail(5000)) if row.get("ref") == ref), None)

    def prune(self) -> None:
        cutoff = time.time() - self.keep_days * 86400
        kept = []
        for row in self.tail(100000):
            try:
                stamp = time.mktime(time.strptime(row["ts"][:19], "%Y-%m-%dT%H:%M:%S"))
            except (KeyError, ValueError):
                continue
            if stamp >= cutoff:
                kept.append(json.dumps(row, ensure_ascii=False))
        if self.path.exists():
            self.path.write_text("".join(line + "\n" for line in kept), encoding="utf-8")
