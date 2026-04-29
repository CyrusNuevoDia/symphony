# ruff: noqa: E501
from __future__ import annotations

import httpx

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
      state {
        name
      }
      branchName
      url
      assignee {
        id
      }
      labels {
        nodes {
          name
        }
      }
      inverseRelations(first: $relationFirst) {
        nodes {
          type
          issue {
            id
            identifier
            state {
              name
            }
          }
        }
      }
      createdAt
      updatedAt
    }
    pageInfo {
      hasNextPage
      endCursor
    }
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
      state {
        name
      }
      branchName
      url
      assignee {
        id
      }
      labels {
        nodes {
          name
        }
      }
      inverseRelations(first: $relationFirst) {
        nodes {
          type
          issue {
            id
            identifier
            state {
              name
            }
          }
        }
      }
      createdAt
      updatedAt
    }
  }
}
"""
VIEWER_QUERY = """
query SymphonyLinearViewer {
  viewer {
    id
  }
}
"""
CREATE_COMMENT_MUTATION = """
mutation SymphonyCreateComment($issueId: String!, $body: String!) {
  commentCreate(input: {issueId: $issueId, body: $body}) {
    success
  }
}
"""
UPDATE_STATE_MUTATION = """
mutation SymphonyUpdateIssueState($issueId: String!, $stateId: String!) {
  issueUpdate(id: $issueId, input: {stateId: $stateId}) {
    success
  }
}
"""
RESOLVE_STATE_ID_QUERY = """
query SymphonyResolveStateId($issueId: String!, $stateName: String!) {
  issue(id: $issueId) {
    team {
      states(filter: {name: {eq: $stateName}}, first: 1) {
        nodes {
          id
        }
      }
    }
  }
}
"""
class LinearTrackerError(RuntimeError):
    pass
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
            connection = self._require_mapping(data.get("issues"), "data.issues")
            issues.extend(self._parse_nodes(connection.get("nodes"), "data.issues.nodes"))
            page = self._require_mapping(connection.get("pageInfo"), "data.issues.pageInfo")
            if not self._require_bool(page.get("hasNextPage"), "data.issues.pageInfo.hasNextPage"):
                return issues
            after = self._require_str(page.get("endCursor"), "data.issues.pageInfo.endCursor")
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
            connection = self._require_mapping(data.get("issues"), "data.issues")
            issues.extend(self._parse_nodes(connection.get("nodes"), "data.issues.nodes"))
        issues.sort(key=lambda issue: order.get(issue.id, len(order)))
        return issues
    async def create_comment(self, issue_id: str, body: str) -> None:
        await self._ensure_authenticated()
        data = await self._graphql(CREATE_COMMENT_MUTATION, {"issueId": issue_id, "body": body})
        self._expect_success(data, "commentCreate", "Linear commentCreate did not succeed")
    async def update_issue_state(self, issue_id: str, state_name: str) -> None:
        await self._ensure_authenticated()
        state_id = await self._resolve_state_id(issue_id, state_name)
        data = await self._graphql(
            UPDATE_STATE_MUTATION,
            {"issueId": issue_id, "stateId": state_id},
        )
        self._expect_success(data, "issueUpdate", "Linear issueUpdate did not succeed")
    async def _ensure_authenticated(self) -> None:
        if self._viewer_checked:
            return
        data = await self._graphql(VIEWER_QUERY, {})
        viewer = self._require_mapping(data.get("viewer"), "data.viewer")
        self._require_str(viewer.get("id"), "data.viewer.id")
        self._viewer_checked = True
    async def _resolve_state_id(self, issue_id: str, state_name: str) -> str:
        data = await self._graphql(
            RESOLVE_STATE_ID_QUERY,
            {"issueId": issue_id, "stateName": state_name},
        )
        issue = self._require_mapping(data.get("issue"), "data.issue")
        team = self._require_mapping(issue.get("team"), "data.issue.team")
        states = self._require_mapping(team.get("states"), "data.issue.team.states")
        nodes = self._require_list(states.get("nodes"), "data.issue.team.states.nodes")
        if not nodes:
            raise LinearTrackerError(
                f"Linear state {state_name!r} was not found for issue {issue_id!r}"
            )
        state = self._require_mapping(nodes[0], "data.issue.team.states.nodes[0]")
        return self._require_str(state.get("id"), "data.issue.team.states.nodes[0].id")
    async def _graphql(self, query: str, variables: dict[str, object]) -> dict[str, object]:
        response = await self._client.post(
            GRAPHQL_ENDPOINT,
            headers={"Authorization": self._api_key, "Content-Type": "application/json"},
            json={"query": query, "variables": variables},
        )
        response.raise_for_status()
        payload = self._require_mapping(response.json(), "response")
        if isinstance(payload.get("errors"), list) and payload["errors"]:
            raise LinearTrackerError(f"Linear GraphQL errors: {payload['errors']!r}")
        return self._require_mapping(payload.get("data"), "response.data")
    def _expect_success(self, data: dict[str, object], field: str, message: str) -> None:
        if self._require_mapping(data.get(field), f"data.{field}").get("success") is not True:
            raise LinearTrackerError(message)
    def _parse_nodes(self, nodes: object, field_name: str) -> list[Issue]:
        return [self._normalize_issue(node) for node in self._require_list(nodes, field_name)]
    def _normalize_issue(self, issue: object) -> Issue:
        payload = self._require_mapping(issue, "issue")
        state = self._require_mapping(payload.get("state"), "issue.state")
        assignee = payload.get("assignee")
        assignee_map = None if assignee is None else self._require_mapping(assignee, "issue.assignee")
        assignee_id = None
        if assignee_map is not None:
            assignee_id = self._optional_str(assignee_map.get("id"), "issue.assignee.id")
        return Issue.model_validate(
            {
                "id": self._require_str(payload.get("id"), "issue.id"),
                "identifier": self._require_str(payload.get("identifier"), "issue.identifier"),
                "title": self._require_str(payload.get("title"), "issue.title"),
                "description": self._optional_str(payload.get("description"), "issue.description"),
                "priority": self._optional_int(payload.get("priority"), "issue.priority"),
                "state": self._require_str(state.get("name"), "issue.state.name"),
                "branch_name": self._optional_str(payload.get("branchName"), "issue.branchName"),
                "url": self._require_str(payload.get("url"), "issue.url"),
                "assignee_id": assignee_id,
                "blocked_by": self._extract_blockers(payload.get("inverseRelations")),
                "labels": self._extract_labels(payload.get("labels")),
                "assigned_to_worker": True,
                "created_at": self._optional_str(payload.get("createdAt"), "issue.createdAt"),
                "updated_at": self._optional_str(payload.get("updatedAt"), "issue.updatedAt"),
            }
        )
    def _extract_labels(self, labels: object) -> list[str]:
        if labels is None:
            return []
        nodes = self._require_list(
            self._require_mapping(labels, "issue.labels").get("nodes"),
            "issue.labels.nodes",
        )
        return [
            self._require_str(
                self._require_mapping(node, "issue.labels.nodes[]").get("name"),
                "issue.labels.nodes[].name",
            ).lower()
            for node in nodes
        ]
    def _extract_blockers(self, inverse_relations: object) -> list[str]:
        if inverse_relations is None:
            return []
        nodes = self._require_list(
            self._require_mapping(inverse_relations, "issue.inverseRelations").get("nodes"),
            "issue.inverseRelations.nodes",
        )
        blocked_by: list[str] = []
        for node in nodes:
            relation = self._require_mapping(node, "issue.inverseRelations.nodes[]")
            relation_type = self._optional_str(
                relation.get("type"),
                "issue.inverseRelations.nodes[].type",
            )
            if relation_type is None or relation_type.strip().lower() != "blocks":
                continue
            blocker = self._require_mapping(
                relation.get("issue"),
                "issue.inverseRelations.nodes[].issue",
            )
            blocked_by.append(
                self._require_str(
                    blocker.get("id"),
                    "issue.inverseRelations.nodes[].issue.id",
                )
            )
        return blocked_by
    @staticmethod
    def _require_mapping(value: object, field_name: str) -> dict[str, object]:
        if isinstance(value, dict):
            return value
        raise LinearTrackerError(f"missing or invalid {field_name}")
    @staticmethod
    def _require_list(value: object, field_name: str) -> list[object]:
        if isinstance(value, list):
            return value
        raise LinearTrackerError(f"missing or invalid {field_name}")
    @staticmethod
    def _require_bool(value: object, field_name: str) -> bool:
        if isinstance(value, bool):
            return value
        raise LinearTrackerError(f"missing or invalid {field_name}")
    @staticmethod
    def _require_str(value: object, field_name: str) -> str:
        if isinstance(value, str) and value:
            return value
        raise LinearTrackerError(f"missing or invalid {field_name}")
    @staticmethod
    def _optional_str(value: object, field_name: str) -> str | None:
        return None if value is None else LinearTracker._require_str(value, field_name)
    @staticmethod
    def _optional_int(value: object, field_name: str) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise LinearTrackerError(f"missing or invalid {field_name}")
        return value
