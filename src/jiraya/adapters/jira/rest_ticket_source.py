"""Production ``TicketSource`` backed by the Jira Cloud REST API.

Implements the same port as the in-memory fake. The ``httpx.Client`` is
injectable so the adapter can be exercised with ``httpx.MockTransport`` without
a live Jira instance.

Uses the current ``/rest/api/3/search/jql`` endpoint (token pagination). The
legacy ``/rest/api/3/search`` endpoint was removed from Jira Cloud in 2025.
"""

from __future__ import annotations

from typing import Any

import httpx

from ...domain import Priority, Ticket, TicketStatus
from ...ports import TicketSource

_DEFAULT_JQL = 'status in ("To Do", "Untriaged") ORDER BY created ASC'
_FIELDS = "summary,description,reporter,priority,status,labels,issuetype,created,updated"

_PRIORITY_BY_NAME = {p.value.lower(): p for p in Priority}
_STATUS_BY_NAME = {s.value.lower(): s for s in TicketStatus}

# Real Jira workflows name their states differently from our canonical domain
# statuses. These aliases let a transition request resolve against whatever the
# target workflow actually calls each state.
_STATUS_ALIASES: dict[TicketStatus, tuple[str, ...]] = {
    TicketStatus.IN_PROGRESS: ("in progress", "start progress", "in development"),
    TicketStatus.NEEDS_REVIEW: ("needs review", "in review", "review", "code review"),
    TicketStatus.TODO: ("to do", "todo", "open", "backlog"),
    TicketStatus.DONE: ("done", "closed", "resolved", "to be deployed"),
    TicketStatus.UNTRIAGED: ("untriaged",),
}


class JiraRestTicketSource(TicketSource):
    """Reads and transitions issues via Jira Cloud's REST API (v3)."""

    def __init__(
        self,
        *,
        base_url: str,
        email: str | None = None,
        api_token: str | None = None,
        jql: str = _DEFAULT_JQL,
        max_results: int = 50,
        page_limit: int = 20,
        client: httpx.Client | None = None,
    ) -> None:
        if not base_url and client is None:
            raise ValueError("Jira base_url is required")
        self._jql = jql
        self._max_results = max_results
        self._page_limit = page_limit
        auth = httpx.BasicAuth(email, api_token) if email and api_token else None
        self._client = client or httpx.Client(
            base_url=base_url.rstrip("/"),
            auth=auth,
            headers={"Accept": "application/json"},
            timeout=30.0,
        )

    def fetch_untriaged(self) -> list[Ticket]:
        """Page through ``/search/jql`` until the result set is exhausted."""
        tickets: list[Ticket] = []
        next_token: str | None = None
        for _ in range(self._page_limit):
            params: dict[str, Any] = {
                "jql": self._jql,
                "fields": _FIELDS,
                "maxResults": self._max_results,
            }
            if next_token:
                params["nextPageToken"] = next_token
            resp = self._client.get("/rest/api/3/search/jql", params=params)
            resp.raise_for_status()
            payload = resp.json()
            tickets.extend(
                self._issue_to_ticket(issue) for issue in payload.get("issues", [])
            )
            next_token = payload.get("nextPageToken")
            if payload.get("isLast", True) or not next_token:
                break
        return tickets

    def get(self, key: str) -> Ticket | None:
        resp = self._client.get(
            f"/rest/api/3/issue/{key}", params={"fields": _FIELDS}
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return self._issue_to_ticket(resp.json())

    def transition(self, key: str, status: TicketStatus) -> Ticket:
        transition_id = self._find_transition_id(key, status)
        if transition_id is None:
            raise ValueError(
                f"No Jira transition to '{status}' available for {key}"
            )
        resp = self._client.post(
            f"/rest/api/3/issue/{key}/transitions",
            json={"transition": {"id": transition_id}},
        )
        resp.raise_for_status()
        updated = self.get(key)
        if updated is None:
            raise KeyError(f"Ticket {key} disappeared after transition")
        return updated

    # -- helpers --------------------------------------------------------------

    def _find_transition_id(self, key: str, status: TicketStatus) -> str | None:
        resp = self._client.get(f"/rest/api/3/issue/{key}/transitions")
        resp.raise_for_status()
        candidates = {status.value.lower(), *_STATUS_ALIASES.get(status, ())}
        for transition in resp.json().get("transitions", []):
            to_name = (transition.get("to") or {}).get("name", "").lower()
            name = transition.get("name", "").lower()
            if to_name in candidates or name in candidates:
                return str(transition.get("id"))
        return None

    def _issue_to_ticket(self, issue: dict[str, Any]) -> Ticket:
        fields = issue.get("fields", {})
        priority_name = (fields.get("priority") or {}).get("name", "")
        status_name = (fields.get("status") or {}).get("name", "")
        reporter = (fields.get("reporter") or {}).get("displayName") or "unknown"
        issue_type = (fields.get("issuetype") or {}).get("name", "")
        key = issue.get("key", "UNKNOWN-0")
        return Ticket(
            key=key,
            project=str(key).split("-", 1)[0],
            summary=fields.get("summary") or "",
            description=_render_description(fields.get("description")),
            reporter=reporter,
            priority=_PRIORITY_BY_NAME.get(priority_name.lower(), Priority.MEDIUM),
            status=_STATUS_BY_NAME.get(status_name.lower(), TicketStatus.TODO),
            labels=tuple(fields.get("labels") or ()),
            issue_type=issue_type,
        )

    def close(self) -> None:
        self._client.close()


def _render_description(value: Any) -> str:
    """Render a Jira description (plain string or ADF document) as text."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):  # Atlassian Document Format
        return _adf_to_text(value).strip()
    return str(value)


def _adf_to_text(node: Any) -> str:
    if isinstance(node, dict):
        if node.get("type") == "text":
            return node.get("text", "")
        parts = [_adf_to_text(child) for child in node.get("content", [])]
        text = "".join(parts)
        if node.get("type") in {"paragraph", "heading"}:
            return text + "\n"
        return text
    if isinstance(node, list):
        return "".join(_adf_to_text(child) for child in node)
    return ""
