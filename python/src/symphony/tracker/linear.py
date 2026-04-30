# ruff: noqa: E501
from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from . import Issue

GRAPHQL_ENDPOINT = "https://api.linear.app/graphql"
ISSUE_PAGE_SIZE = 50

POLL_QUERY = """
query SymphonyLinearPoll($projectSlug: String!, $stateNames: [String!]!, $first: Int!, $relationFirst: Int!, $after: String) {
  issues(filter: {project: {slugId: {eq: $projectSlug}}, state: {name: {in: $stateNames}}}, first: $first, after: $after) {
    nodes {
      id
      identifier
      title
      description
      priority
      state { name }
      branchName
      url
      assignee { id }
      labels { nodes { name } }
      inverseRelations(first: $relationFirst) {
        nodes {
          type
          issue { id identifier state { name } }
        }
      }
      createdAt
      updatedAt
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

ISSUES_BY_ID_QUERY = """
query SymphonyLinearIssuesById($ids: [ID!]!, $first: Int!, $relationFirst: Int!) {
  issues(filter: {id: {in: $ids}}, first: $first) {
    nodes {
      id
      identifier
      title
      description
      priority
      state { name }
      branchName
      url
      assignee { id }
      labels { nodes { name } }
      inverseRelations(first: $relationFirst) {
        nodes {
          type
          issue { id identifier state { name } }
        }
      }
      createdAt
      updatedAt
    }
  }
}
"""

VIEWER_QUERY = "query SymphonyLinearViewer { viewer { id } }"

CREATE_COMMENT_MUTATION = """
mutation SymphonyCreateComment($issueId: String!, $body: String!) {
  commentCreate(input: {issueId: $issueId, body: $body}) { success }
}
"""

UPDATE_STATE_MUTATION = """
mutation SymphonyUpdateIssueState($issueId: String!, $stateId: String!) {
  issueUpdate(id: $issueId, input: {stateId: $stateId}) { success }
}
"""

RESOLVE_STATE_ID_QUERY = """
query SymphonyResolveStateId($issueId: String!, $stateName: String!) {
  issue(id: $issueId) {
    team {
      states(filter: {name: {eq: $stateName}}, first: 1) { nodes { id } }
    }
  }
}
"""


class LinearTrackerError(RuntimeError):
    pass


class _Aliased(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class _State(_Aliased):
    name: str


class _Assignee(_Aliased):
    id: str


class _Label(_Aliased):
    name: str


class _Labels(_Aliased):
    nodes: list[_Label] = Field(default_factory=list)


class _RelatedIssue(_Aliased):
    id: str


class _InverseRelation(_Aliased):
    type: str | None = None
    issue: _RelatedIssue


class _InverseRelations(_Aliased):
    nodes: list[_InverseRelation] = Field(default_factory=list)


class _IssueNode(_Aliased):
    id: str
    identifier: str
    title: str
    description: str | None = None
    priority: int | None = None
    state: _State
    branch_name: str | None = Field(default=None, alias="branchName")
    url: str
    assignee: _Assignee | None = None
    labels: _Labels | None = None
    inverse_relations: _InverseRelations | None = Field(default=None, alias="inverseRelations")
    created_at: str | None = Field(default=None, alias="createdAt")
    updated_at: str | None = Field(default=None, alias="updatedAt")

    def to_issue(self) -> Issue:
        return Issue.model_validate(
            {
                "id": self.id,
                "identifier": self.identifier,
                "title": self.title,
                "description": self.description,
                "priority": self.priority,
                "state": self.state.name,
                "branch_name": self.branch_name,
                "url": self.url,
                "assignee_id": self.assignee.id if self.assignee else None,
                "blocked_by": [
                    rel.issue.id
                    for rel in (self.inverse_relations.nodes if self.inverse_relations else [])
                    if (rel.type or "").strip().lower() == "blocks"
                ],
                "labels": [label.name.lower() for label in (self.labels.nodes if self.labels else [])],
                "assigned_to_worker": True,
                "created_at": self.created_at,
                "updated_at": self.updated_at,
            }
        )


class _PageInfo(_Aliased):
    has_next_page: bool = Field(alias="hasNextPage")
    end_cursor: str | None = Field(default=None, alias="endCursor")


class _IssueConnection(_Aliased):
    nodes: list[_IssueNode] = Field(default_factory=list)
    page_info: _PageInfo | None = Field(default=None, alias="pageInfo")


class LinearTracker:
    def __init__(
        self,
        *,
        api_key: str,
        project_slug: str,
        active_states: list[str],
        terminal_states: list[str],
        client: httpx.AsyncClient | None = None,
    ) -> None:
        del terminal_states
        self._api_key = api_key
        self._project_slug = project_slug
        self._active_states = list(dict.fromkeys(active_states))
        self._client = client or httpx.AsyncClient()
        self._owns_client = client is None
        self._viewer_checked = False

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def fetch_candidate_issues(self) -> list[Issue]:
        return await self.fetch_issues_by_states(self._active_states)

    async def fetch_issues_by_states(self, state_names: list[str]) -> list[Issue]:
        states = list(dict.fromkeys(state_names))
        if not states:
            return []
        await self._ensure_authenticated()
        issues: list[Issue] = []
        after: str | None = None
        while True:
            data = await self._graphql(
                POLL_QUERY,
                {
                    "projectSlug": self._project_slug,
                    "stateNames": states,
                    "first": ISSUE_PAGE_SIZE,
                    "relationFirst": ISSUE_PAGE_SIZE,
                    "after": after,
                },
            )
            connection = _IssueConnection.model_validate(data.get("issues") or {})
            issues.extend(node.to_issue() for node in connection.nodes)
            page = connection.page_info
            if page is None or not page.has_next_page:
                return issues
            if not page.end_cursor:
                raise LinearTrackerError("missing or invalid data.issues.pageInfo.endCursor")
            after = page.end_cursor

    async def fetch_issue_states_by_ids(self, issue_ids: list[str]) -> list[Issue]:
        ids = list(dict.fromkeys(issue_ids))
        if not ids:
            return []
        await self._ensure_authenticated()
        order = {issue_id: index for index, issue_id in enumerate(ids)}
        issues: list[Issue] = []
        for start in range(0, len(ids), ISSUE_PAGE_SIZE):
            batch = ids[start : start + ISSUE_PAGE_SIZE]
            data = await self._graphql(
                ISSUES_BY_ID_QUERY,
                {"ids": batch, "first": len(batch), "relationFirst": ISSUE_PAGE_SIZE},
            )
            connection = _IssueConnection.model_validate(data.get("issues") or {})
            issues.extend(node.to_issue() for node in connection.nodes)
        issues.sort(key=lambda issue: order.get(issue.id, len(order)))
        return issues

    async def create_comment(self, issue_id: str, body: str) -> None:
        await self._ensure_authenticated()
        data = await self._graphql(CREATE_COMMENT_MUTATION, {"issueId": issue_id, "body": body})
        self._expect_success(data, "commentCreate")

    async def update_issue_state(self, issue_id: str, state_name: str) -> None:
        await self._ensure_authenticated()
        state_id = await self._resolve_state_id(issue_id, state_name)
        data = await self._graphql(
            UPDATE_STATE_MUTATION,
            {"issueId": issue_id, "stateId": state_id},
        )
        self._expect_success(data, "issueUpdate")

    async def _ensure_authenticated(self) -> None:
        if self._viewer_checked:
            return
        data = await self._graphql(VIEWER_QUERY, {})
        viewer = data.get("viewer") or {}
        if not isinstance(viewer, dict) or not viewer.get("id"):
            raise LinearTrackerError("missing or invalid data.viewer.id")
        self._viewer_checked = True

    async def _resolve_state_id(self, issue_id: str, state_name: str) -> str:
        data: Any = await self._graphql(
            RESOLVE_STATE_ID_QUERY,
            {"issueId": issue_id, "stateName": state_name},
        )
        try:
            state_id = data["issue"]["team"]["states"]["nodes"][0]["id"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LinearTrackerError(
                f"Linear state {state_name!r} was not found for issue {issue_id!r}"
            ) from exc
        if not isinstance(state_id, str) or not state_id:
            raise LinearTrackerError(f"invalid state id for {state_name!r}")
        return state_id

    async def _graphql(self, query: str, variables: dict[str, object]) -> dict[str, object]:
        response = await self._client.post(
            GRAPHQL_ENDPOINT,
            headers={"Authorization": self._api_key, "Content-Type": "application/json"},
            json={"query": query, "variables": variables},
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise LinearTrackerError("response is not a JSON object")
        if isinstance(payload.get("errors"), list) and payload["errors"]:
            raise LinearTrackerError(f"Linear GraphQL errors: {payload['errors']!r}")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise LinearTrackerError("missing or invalid response.data")
        return data

    def _expect_success(self, data: dict[str, object], field: str) -> None:
        result = data.get(field)
        if not isinstance(result, dict) or result.get("success") is not True:
            raise LinearTrackerError(f"Linear {field} did not succeed")
