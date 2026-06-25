from __future__ import annotations

import json

import httpx
import pytest

from jiraya.adapters.jira import JiraRestTicketSource
from jiraya.domain import Priority, TicketStatus

_ADF = {
    "type": "doc",
    "version": 1,
    "content": [
        {"type": "paragraph",
         "content": [{"type": "text", "text": "Steps to reproduce the bug."}]},
        {"type": "paragraph", "content": [{"type": "text", "text": "Second line."}]},
    ],
}


def _issue(key: str, status_name: str = "Untriaged", issue_type: str = "Bug",
           description=_ADF):
    return {
        "key": key,
        "fields": {
            "summary": "Login fails",
            "description": description,
            "reporter": {"displayName": "Alice"},
            "priority": {"name": "High"},
            "status": {"name": status_name},
            "labels": ["bug"],
            "issuetype": {"name": issue_type},
        },
    }


def _paged_client(pages: list[dict]) -> httpx.Client:
    """Serve /search/jql across multiple token-paginated pages."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/rest/api/3/search/jql"
        idx = calls["n"]
        calls["n"] += 1
        return httpx.Response(200, json=pages[idx])

    return httpx.Client(transport=httpx.MockTransport(handler), base_url="https://x")


def test_fetch_untriaged_uses_search_jql_and_renders_adf():
    page = {"issues": [_issue("PROJ-1")], "isLast": True}
    src = JiraRestTicketSource(base_url="https://x", client=_paged_client([page]))
    tickets = src.fetch_untriaged()
    assert len(tickets) == 1
    t = tickets[0]
    assert t.key == "PROJ-1"
    assert t.project == "PROJ"
    assert t.reporter == "Alice"
    assert t.priority is Priority.HIGH
    assert t.status is TicketStatus.UNTRIAGED
    assert t.labels == ("bug",)
    assert t.issue_type == "Bug"
    assert "Steps to reproduce the bug." in t.description
    assert "Second line." in t.description


def test_fetch_untriaged_follows_token_pagination():
    pages = [
        {"issues": [_issue("PROJ-1")], "nextPageToken": "tok2", "isLast": False},
        {"issues": [_issue("PROJ-2")], "nextPageToken": "tok3", "isLast": False},
        {"issues": [_issue("PROJ-3")], "isLast": True},
    ]
    src = JiraRestTicketSource(base_url="https://x", client=_paged_client(pages))
    tickets = src.fetch_untriaged()
    assert [t.key for t in tickets] == ["PROJ-1", "PROJ-2", "PROJ-3"]


def test_fetch_handles_null_description_and_reporter():
    page = {"issues": [{
        "key": "PROJ-9",
        "fields": {
            "summary": "No body",
            "description": None,
            "reporter": None,
            "priority": None,
            "status": {"name": "Untriaged"},
            "labels": None,
            "issuetype": {"name": "Epic"},
        },
    }], "isLast": True}
    src = JiraRestTicketSource(base_url="https://x", client=_paged_client([page]))
    t = src.fetch_untriaged()[0]
    assert t.description == ""
    assert t.reporter == "unknown"
    assert t.priority is Priority.MEDIUM
    assert t.labels == ()
    assert t.issue_type == "Epic"


def _workflow_client(state: dict) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/rest/api/3/issue/PROJ-1/transitions":
            if request.method == "GET":
                # Real-world workflow names: "In Progress" via a "Start work" action.
                return httpx.Response(200, json={"transitions": [
                    {"id": "21", "name": "Start work", "to": {"name": "In Progress"}},
                    {"id": "31", "name": "Review", "to": {"name": "In Review"}},
                ]})
            body = json.loads(request.content)
            assert body["transition"]["id"] == "21"
            state["status"] = "In Progress"
            return httpx.Response(204)
        if path == "/rest/api/3/issue/PROJ-1":
            return httpx.Response(200, json=_issue("PROJ-1", state["status"]))
        if path == "/rest/api/3/issue/MISSING":
            return httpx.Response(404, json={"errorMessages": ["not found"]})
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler), base_url="https://x")


def test_transition_resolves_in_progress_alias():
    src = JiraRestTicketSource(base_url="https://x", client=_workflow_client({"status": "Untriaged"}))
    updated = src.transition("PROJ-1", TicketStatus.IN_PROGRESS)
    assert updated.status is TicketStatus.IN_PROGRESS


def test_needs_review_maps_to_in_review_alias():
    src = JiraRestTicketSource(base_url="https://x", client=_workflow_client({"status": "Untriaged"}))
    # NEEDS_REVIEW should resolve against a workflow that calls it "In Review".
    assert src._find_transition_id("PROJ-1", TicketStatus.NEEDS_REVIEW) == "31"


def test_transition_without_match_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/transitions"):
            return httpx.Response(200, json={"transitions": []})
        return httpx.Response(200, json=_issue("PROJ-1"))

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://x")
    src = JiraRestTicketSource(base_url="https://x", client=client)
    with pytest.raises(ValueError):
        src.transition("PROJ-1", TicketStatus.DONE)


def test_get_returns_none_on_404():
    src = JiraRestTicketSource(base_url="https://x", client=_workflow_client({"status": "Untriaged"}))
    assert src.get("MISSING") is None
