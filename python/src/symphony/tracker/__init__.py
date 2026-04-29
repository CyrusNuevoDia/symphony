from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field


class Issue(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    identifier: str
    title: str
    description: str | None = None
    state: str
    priority: int | None = None
    branch_name: str | None = None
    url: str
    assignee_id: str | None = None
    blocked_by: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    assigned_to_worker: bool = True
    created_at: datetime | None = None
    updated_at: datetime | None = None


class Tracker(Protocol):
    async def fetch_candidate_issues(self) -> list[Issue]: ...

    async def fetch_issues_by_states(self, state_names: list[str]) -> list[Issue]: ...

    async def fetch_issue_states_by_ids(self, issue_ids: list[str]) -> list[Issue]: ...

    async def create_comment(self, issue_id: str, body: str) -> None: ...

    async def update_issue_state(self, issue_id: str, state_name: str) -> None: ...


__all__ = ["Issue", "Tracker"]
