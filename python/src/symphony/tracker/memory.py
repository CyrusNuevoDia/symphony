from __future__ import annotations

from symphony.tracker import Issue


def _normalize_state(state: str) -> str:
    return state.strip().lower()


class MemoryTracker:
    def __init__(self) -> None:
        self._issues: dict[str, Issue] = {}
        self._comments_log: list[tuple[str, str]] = []
        self._state_updates_log: list[tuple[str, str]] = []

    async def fetch_candidate_issues(self) -> list[Issue]:
        return [issue.model_copy(deep=True) for issue in self._issues.values()]

    async def fetch_issues_by_states(self, state_names: list[str]) -> list[Issue]:
        wanted = {_normalize_state(state) for state in state_names}
        return [
            issue.model_copy(deep=True)
            for issue in self._issues.values()
            if _normalize_state(issue.state) in wanted
        ]

    async def fetch_issue_states_by_ids(self, issue_ids: list[str]) -> list[Issue]:
        wanted = set(issue_ids)
        return [
            issue.model_copy(deep=True) for issue in self._issues.values() if issue.id in wanted
        ]

    async def create_comment(self, issue_id: str, body: str) -> None:
        self._comments_log.append((issue_id, body))

    async def update_issue_state(self, issue_id: str, state_name: str) -> None:
        self._state_updates_log.append((issue_id, state_name))
        self.set_state(issue_id, state_name)

    def add_issue(self, issue: Issue) -> None:
        self._issues[issue.id] = issue.model_copy(deep=True)

    def set_state(self, issue_id: str, state_name: str) -> None:
        self._issues[issue_id].state = state_name

    @property
    def comments(self) -> list[tuple[str, str]]:
        return list(self._comments_log)

    @property
    def state_updates(self) -> list[tuple[str, str]]:
        return list(self._state_updates_log)
