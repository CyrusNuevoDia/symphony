from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest
from codex_app_server_sdk import ChatResult, ConversationStep
from fastactor import Runtime

from symphony.config import Settings
from symphony.issue_agent import IssueAgent
from symphony.tracker import Issue
from symphony.workflow import Workflow


class FakeCodexSession:
    start_calls: ClassVar[int] = 0
    run_calls: ClassVar[int] = 0
    stop_calls: ClassVar[int] = 0
    event_calls: ClassVar[int] = 0
    prompts: ClassVar[list[str]] = []

    def __init__(self, *, workspace: Path | str, settings_codex) -> None:
        self.workspace = Path(workspace)
        self.settings_codex = settings_codex
        self._thread_id = "thread-fake"

    @classmethod
    def reset(cls) -> None:
        cls.start_calls = 0
        cls.run_calls = 0
        cls.stop_calls = 0
        cls.event_calls = 0
        cls.prompts = []

    async def start(self) -> None:
        type(self).start_calls += 1

    async def run_turn(self, prompt: str, on_event=None) -> ChatResult:
        type(self).run_calls += 1
        type(self).prompts.append(prompt)
        for index in range(2):
            step = ConversationStep(
                thread_id=self._thread_id,
                turn_id="turn-fake",
                item_id=f"item-{index}",
                step_type="codex",
                item_type="agentMessage",
                text=f"event-{index}",
            )
            if on_event is not None:
                type(self).event_calls += 1
                on_event(step)
        return ChatResult(
            thread_id=self._thread_id,
            turn_id="turn-fake",
            final_text="done",
            raw_events=[],
            assistant_item_id="item-1",
            completion_source="item_completed",
        )

    async def stop(self) -> None:
        type(self).stop_calls += 1

    @property
    def thread_id(self) -> str:
        return self._thread_id


class FakeParent:
    def __init__(self, *, refreshed_issue: Issue) -> None:
        self.refreshed_issue = refreshed_issue
        self.casts: list[tuple[str, str, ConversationStep]] = []
        self.calls: list[tuple[tuple[str, str], float]] = []

    def cast(self, request: tuple[str, str, ConversationStep]) -> None:
        self.casts.append(request)

    async def call(self, request: tuple[str, str], timeout: float = 5.0) -> Issue:
        self.calls.append((request, timeout))
        return self.refreshed_issue


def _settings(tmp_path: Path) -> Settings:
    return Settings.model_validate(
        {
            "tracker": {"kind": "memory", "project_slug": "demo", "active_states": ["Todo"]},
            "polling": {"interval_ms": 1000},
            "workspace": {"root": str(tmp_path / "workspaces")},
            "agent": {"max_concurrent_agents": 1, "max_turns": 3, "max_retry_backoff_ms": 1000},
            "codex": {
                "command": ["codex", "app-server"],
                "approval_policy": "never",
                "thread_sandbox": "workspace-write",
                "turn_sandbox_policy": {"type": "workspaceWrite"},
            },
        }
    )


def _issue(*, state: str = "Todo") -> Issue:
    return Issue(
        id="issue-123",
        identifier="ENG-123",
        title="Example issue",
        description="Example description",
        state=state,
        url="https://example.com/issues/ENG-123",
    )


@pytest.mark.anyio
async def test_issue_agent_runs_single_turn_and_stops(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from symphony import issue_agent as issue_agent_module

    FakeCodexSession.reset()
    ensure_calls: list[tuple[Issue, Settings]] = []
    workspace = tmp_path / "ENG-123"

    async def fake_ensure_worktree(issue: Issue, settings: Settings) -> Path:
        ensure_calls.append((issue, settings))
        workspace.mkdir()
        return workspace

    monkeypatch.setattr(issue_agent_module, "CodexSession", FakeCodexSession)
    monkeypatch.setattr(issue_agent_module, "ensure_worktree", fake_ensure_worktree)

    issue = _issue()
    settings = _settings(tmp_path)
    workflow = Workflow(
        config={},
        prompt_template=(
            "Issue {{ issue.identifier }}"
            "{% if attempt %} retry {{ attempt }}{% endif %}"
        ),
    )
    parent = FakeParent(refreshed_issue=_issue(state="Done"))

    async with Runtime():
        agent = await IssueAgent.start(
            issue=issue,
            workflow=workflow,
            settings=settings,
            parent=parent,
        )
        await agent.stopped()

    assert ensure_calls == [(issue, settings)]
    assert agent.turn == 1
    assert FakeCodexSession.start_calls == 1
    assert FakeCodexSession.run_calls == 1
    assert FakeCodexSession.stop_calls == 1
    assert FakeCodexSession.event_calls == 2
    assert len(parent.casts) == 2
    assert parent.casts[0][0:2] == ("codex_event", issue.id)
    assert parent.calls == [(("refresh_issue", issue.id), 30.0)]
    assert FakeCodexSession.prompts == ["Issue ENG-123"]
