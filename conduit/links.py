"""Links a person clicks: sign in to a site, or approve a delete. Each link carries one random
token bound to one connector or one exact call, valid for 15 minutes; nothing else is in the URL.
Opening a link only shows a page; its button does the work, so a link preview or an agent that
fetches the URL triggers nothing."""

from __future__ import annotations

import html
import secrets
import time

APPROVAL_WINDOW = 600


class Links:
    def __init__(self, ttl: float = 900):
        self.ttl = ttl
        self._links: dict[str, dict] = {}

    def _prune(self) -> None:
        now = time.time()
        for token in [t for t, link in self._links.items() if link["expires"] < now]:
            del self._links[token]

    def _new(self, **fields) -> str:
        self._prune()
        token = secrets.token_urlsafe(16)
        self._links[token] = {"expires": time.time() + self.ttl, "state": "new", **fields}
        return token

    def sign_in(self, connector_id: str) -> str:
        self._prune()
        for token, link in self._links.items():
            if link["kind"] == "sign_in" and link["connector_id"] == connector_id and link["state"] == "new":
                return token
        return self._new(kind="sign_in", connector_id=connector_id)

    def approval(self, tool: str, digest: str, preview: dict) -> str:
        self._prune()
        for token, link in self._links.items():
            if (link["kind"] == "approval" and (link["tool"], link["digest"]) == (tool, digest)
                    and link["state"] == "new"):
                return token
        return self._new(kind="approval", tool=tool, digest=digest, preview=preview)

    def get(self, token: str, kind: str) -> dict | None:
        self._prune()
        link = self._links.get(token)
        return link if link is not None and link["kind"] == kind else None

    def mark(self, token: str, state: str) -> None:
        link = self._links.get(token)
        if link is not None:
            link["state"] = state
            link["at"] = time.time()

    def consume_approval(self, tool: str, digest: str) -> bool:
        """True once, for a call that matches a delete the person approved in the last 10 minutes."""
        self._prune()
        for link in self._links.values():
            if (link["kind"] == "approval" and (link["tool"], link["digest"]) == (tool, digest)
                    and link["state"] == "approved" and time.time() - link["at"] <= APPROVAL_WINDOW):
                link["state"] = "used"
                return True
        return False


def page(title: str, paragraphs: list[str], *, form: list[tuple[str, str]] | None = None,
         refresh: int | None = None) -> str:
    """A plain page in the dashboard's style. Every value is escaped; buttons are POST forms."""
    head = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    body = "".join(f"<p>{html.escape(text)}</p>" for text in paragraphs)
    buttons = "".join(
        f'<form method="post" action="{html.escape(action)}">'
        f'<button type="submit">{html.escape(label)}</button></form> '
        for action, label in (form or []))
    return ("<!doctype html><html><head><meta charset=\"utf-8\"><title>" + html.escape(title) + "</title>"
            + head + '<link rel="stylesheet" href="/static/style.css"></head>'
            + "<body><main>"
            + f"<h1>{html.escape(title)}</h1>{body}{buttons}</main></body></html>")
