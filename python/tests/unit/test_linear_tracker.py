from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from symphony.tracker.linear import LinearTracker


def _response(data: dict[str, object]) -> httpx.Response:
    return httpx.Response(200, json={"data": data})


def _payload(request: httpx.Request) -> dict[str, object]:
    return json.loads(request.content.decode("utf-8"))


def _variables(payload: dict[str, object]) -> dict[str, object]:
    variables = payload["variables"]
    assert isinstance(variables, dict)
    return variables


def _issue_node(issue_id: str, *, state: str, priority: int = 2) -> dict[str, object]:
    return {
        "id": issue_id,
        "identifier": f"ENG-{issue_id[-1]}",
        "title": f"Issue {issue_id}",
        "description": f"Description for {issue_id}",
        "priority": priority,
        "state": {"name": state},
        "branchName": f"branch-{issue_id}",
        "url": f"https://linear.example/{issue_id}",
        "assignee": {"id": "user-1"},
        "labels": {"nodes": [{"name": "Backend"}, {"name": "Urgent"}]},
        "inverseRelations": {
            "nodes": [
                {
                    "type": "blocks",
                    "issue": {
                        "id": "issue-2",
                        "identifier": "ENG-2",
                        "state": {"name": "In Progress"},
                    },
                }
            ]
        },
        "createdAt": "2026-04-28T12:34:56Z",
        "updatedAt": "2026-04-29T12:34:56Z",
    }


@pytest.mark.anyio
async def test_fetch_candidate_issues_paginates_and_merges_pages() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = _payload(request)
        requests.append(payload)
        assert request.headers["Authorization"] == "linear-key"
        query = str(payload["query"])
        variables = _variables(payload)

        if "SymphonyLinearViewer" in query:
            return _response({"viewer": {"id": "viewer-1"}})
        if variables["after"] is None:
            return _response(
                {
                    "issues": {
                        "nodes": [_issue_node("issue-1", state="Todo")],
                        "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
                    }
                }
            )
        return _response(
            {
                "issues": {
                    "nodes": [_issue_node("issue-3", state="In Progress")],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        tracker = LinearTracker(
            api_key="linear-key",
            project_slug="proj",
            active_states=["Todo", "In Progress"],
            terminal_states=["Done"],
            client=client,
        )

        issues = await tracker.fetch_candidate_issues()

    assert [issue.id for issue in issues] == ["issue-1", "issue-3"]
    assert _variables(requests[1]) == {
        "projectSlug": "proj",
        "stateNames": ["Todo", "In Progress"],
        "first": 50,
        "relationFirst": 50,
        "after": None,
    }
    assert _variables(requests[2])["after"] == "cursor-1"


@pytest.mark.anyio
async def test_create_comment_posts_expected_mutation() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = _payload(request)
        requests.append(payload)
        query = str(payload["query"])
        if "SymphonyLinearViewer" in query:
            return _response({"viewer": {"id": "viewer-1"}})
        return _response({"commentCreate": {"success": True}})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        tracker = LinearTracker(
            api_key="linear-key",
            project_slug="proj",
            active_states=["Todo"],
            terminal_states=["Done"],
            client=client,
        )

        await tracker.create_comment("issue-7", "Workpad updated")

    assert "SymphonyCreateComment" in str(requests[1]["query"])
    assert _variables(requests[1]) == {"issueId": "issue-7", "body": "Workpad updated"}


@pytest.mark.anyio
async def test_update_issue_state_resolves_state_id_then_updates_issue() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = _payload(request)
        requests.append(payload)
        query = str(payload["query"])
        if "SymphonyLinearViewer" in query:
            return _response({"viewer": {"id": "viewer-1"}})
        if "SymphonyResolveStateId" in query:
            return _response({"issue": {"team": {"states": {"nodes": [{"id": "state-9"}]}}}})
        return _response({"issueUpdate": {"success": True}})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        tracker = LinearTracker(
            api_key="linear-key",
            project_slug="proj",
            active_states=["Todo"],
            terminal_states=["Done"],
            client=client,
        )

        await tracker.update_issue_state("issue-4", "Done")

    assert "SymphonyResolveStateId" in str(requests[1]["query"])
    assert _variables(requests[1]) == {"issueId": "issue-4", "stateName": "Done"}
    assert "SymphonyUpdateIssueState" in str(requests[2]["query"])
    assert _variables(requests[2]) == {"issueId": "issue-4", "stateId": "state-9"}


@pytest.mark.anyio
async def test_fetch_issue_states_by_ids_normalizes_issue_fields() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = _payload(request)
        query = str(payload["query"])
        if "SymphonyLinearViewer" in query:
            return _response({"viewer": {"id": "viewer-1"}})
        return _response({"issues": {"nodes": [_issue_node("issue-1", state="Todo", priority=3)]}})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        tracker = LinearTracker(
            api_key="linear-key",
            project_slug="proj",
            active_states=["Todo"],
            terminal_states=["Done"],
            client=client,
        )

        issues = await tracker.fetch_issue_states_by_ids(["issue-1"])

    issue = issues[0]
    assert issue.identifier == "ENG-1"
    assert issue.title == "Issue issue-1"
    assert issue.description == "Description for issue-1"
    assert issue.priority == 3
    assert issue.state == "Todo"
    assert issue.branch_name == "branch-issue-1"
    assert issue.url == "https://linear.example/issue-1"
    assert issue.assignee_id == "user-1"
    assert issue.labels == ["backend", "urgent"]
    assert issue.blocked_by == ["issue-2"]
    assert issue.assigned_to_worker is True
    assert issue.created_at == datetime(2026, 4, 28, 12, 34, 56, tzinfo=UTC)
    assert issue.updated_at == datetime(2026, 4, 29, 12, 34, 56, tzinfo=UTC)
