from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar, cast
from uuid import uuid4

import anyio
import pytest
from codex_app_server_sdk import ChatResult, ConversationStep
from fastactor import Runtime
from fastactor.otp import DynamicSupervisor, Registry

from symphony.config import Settings
from symphony.orchestrator import (
    CONTINUATION_RETRY_DELAY_MS,
    FAILURE_RETRY_BASE_MS,
    Orchestrator,
    failure_retry_delay,
)
from symphony.tracker import Issue
from symphony.tracker.memory import MemoryTracker
from symphony.workflow import load
from symphony.workflow_store import WorkflowStore


class FakeCodexSession:
    scripts: ClassVar[dict[str, list[str]]] = {}
    callbacks: ClassVar[dict[str, list[Callable[[], None] | None]]] = {}
    release_events: ClassVar[dict[str, anyio.Event]] = {}
    entered_events: ClassVar[dict[str, anyio.Event]] = {}
    start_calls: ClassVar[dict[str, int]] = {}
    run_calls: ClassVar[dict[str, int]] = {}
    stop_calls: ClassVar[dict[str, int]] = {}
    active_runs: ClassVar[int] = 0
    max_active_runs: ClassVar[int] = 0

    def __init__(self, *, workspace: Path | str, settings_codex: object) -> None:
        del settings_codex
        self.workspace = Path(workspace)
        self.key = self.workspace.name

    @classmethod
    def reset(cls) -> None:
        cls.scripts = {}
        cls.callbacks = {}
        cls.release_events = {}
        cls.entered_events = {}
        cls.start_calls = {}
        cls.run_calls = {}
        cls.stop_calls = {}
        cls.active_runs = 0
        cls.max_active_runs = 0

    @classmethod
    def configure(
        cls,
        key: str,
        *,
        scripts: list[str],
        callbacks: list[Callable[[], None] | None] | None = None,
        release_event: anyio.Event | None = None,
    ) -> None:
        cls.scripts[key] = list(scripts)
        cls.callbacks[key] = list(callbacks or [None] * len(scripts))
        cls.entered_events[key] = anyio.Event()
        if release_event is not None:
            cls.release_events[key] = release_event

    async def start(self) -> None:
        type(self).start_calls[self.key] = type(self).start_calls.get(self.key, 0) + 1

    async def run_turn(self, prompt: str, on_event=None) -> ChatResult:
        del prompt
        cls = type(self)
        cls.run_calls[self.key] = cls.run_calls.get(self.key, 0) + 1
        cls.entered_events.setdefault(self.key, anyio.Event()).set()
        cls.active_runs += 1
        cls.max_active_runs = max(cls.max_active_runs, cls.active_runs)

        script = cls.scripts.setdefault(self.key, ["return"])
        callbacks = cls.callbacks.setdefault(self.key, [None])
        action = script.pop(0) if script else "return"
        callback = callbacks.pop(0) if callbacks else None

        try:
            await anyio.sleep(0)
            if on_event is not None:
                on_event(
                    ConversationStep(
                        thread_id=f"thread-{self.key}",
                        turn_id=f"turn-{self.key}",
                        item_id=f"item-{self.key}",
                        step_type="codex",
                        item_type="agentMessage",
                        text=f"event-{self.key}",
                    )
                )
            if action == "hold":
                await cls.release_events[self.key].wait()
            elif action == "crash":
                await anyio.sleep(0.01)
                raise RuntimeError(f"boom-{self.key}")
            await anyio.sleep(0.01)
            if callback is not None:
                callback()
            return ChatResult(
                thread_id=f"thread-{self.key}",
                turn_id=f"turn-{self.key}",
                final_text=f"done-{self.key}",
                raw_events=[],
                assistant_item_id=f"item-{self.key}",
                completion_source="item_completed",
            )
        finally:
            cls.active_runs -= 1

    async def stop(self) -> None:
        type(self).stop_calls[self.key] = type(self).stop_calls.get(self.key, 0) + 1


def _issue(
    number: int,
    *,
    state: str = "Todo",
    priority: int | None = None,
    created_at: datetime | None = None,
) -> Issue:
    return Issue(
        id=f"issue-{number}",
        identifier=f"ENG-{number}",
        title=f"Issue {number}",
        description=f"Description {number}",
        state=state,
        priority=priority,
        url=f"https://example.com/issues/ENG-{number}",
        created_at=created_at,
        updated_at=created_at,
    )


def _write_workflow(
    path: Path,
    *,
    workspace_root: Path,
    poll_interval_ms: int = 20,
    max_concurrent_agents: int = 1,
    max_turns: int = 1,
    per_state_limits: dict[str, int] | None = None,
) -> None:
    lines = [
        "---",
        "tracker:",
        "  kind: memory",
        "  active_states:",
        "    - Todo",
        "  terminal_states:",
        "    - Done",
        "    - Closed",
        "    - Cancelled",
        "    - Canceled",
        "    - Duplicate",
        "polling:",
        f"  interval_ms: {poll_interval_ms}",
        "workspace:",
        f"  root: {workspace_root}",
        "agent:",
        f"  max_concurrent_agents: {max_concurrent_agents}",
        f"  max_turns: {max_turns}",
        "  max_retry_backoff_ms: 600000",
    ]
    if per_state_limits:
        lines.append("  max_concurrent_agents_by_state:")
        lines.extend(
            f"    {state}: {limit}" for state, limit in per_state_limits.items()
        )
    lines.extend(
        [
            "codex:",
            "  command:",
            "    - codex",
            "    - app-server",
            "  approval_policy: never",
            "  thread_sandbox: workspace-write",
            "---",
            "",
            "Issue {{ issue.identifier }}",
            "",
        ]
    )
    path.write_text("\n".join(lines))


async def _start_orchestrator(
    *,
    tracker: MemoryTracker,
    workflow_path: Path,
) -> Orchestrator:
    workflow = load(workflow_path)
    settings = Settings.from_workflow_config(workflow.config)
    registry_name = f"test.agents.{uuid4().hex}"
    await Registry.new(registry_name, "unique")
    agents_sup = await DynamicSupervisor.start_link(
        max_children=settings.agent.max_concurrent_agents,
    )
    workflow_store = await WorkflowStore.start_link(path=workflow_path, poll_interval_ms=20)
    return await Orchestrator.start_link(
        tracker=tracker,
        workflow_store=workflow_store,
        agents_sup=agents_sup,
        registry=registry_name,
        settings=settings,
    )


async def _wait_until(
    predicate: Callable[[], bool],
    *,
    timeout: float = 2.0,
    interval: float = 0.01,
) -> None:
    with anyio.fail_after(timeout):
        while not predicate():
            await anyio.sleep(interval)


async def _snapshot(orchestrator: Orchestrator) -> dict[str, object]:
    return cast(dict[str, object], await orchestrator.call("snapshot"))


@pytest.mark.anyio
async def test_polling_cycle_dispatches_one_issue(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from symphony import issue_agent as issue_agent_module

    FakeCodexSession.reset()
    monkeypatch.setattr(issue_agent_module, "CodexSession", FakeCodexSession)

    workflow_path = tmp_path / "WORKFLOW.md"
    workspace_root = tmp_path / "workspaces"
    _write_workflow(
        workflow_path,
        workspace_root=workspace_root,
        max_concurrent_agents=1,
    )

    release = anyio.Event()
    FakeCodexSession.configure("ENG-1", scripts=["hold"], release_event=release)
    tracker = MemoryTracker()
    tracker.add_issue(_issue(1))

    async with Runtime():
        orchestrator = await _start_orchestrator(tracker=tracker, workflow_path=workflow_path)

        await _wait_until(lambda: FakeCodexSession.run_calls.get("ENG-1", 0) == 1)
        snapshot = await _snapshot(orchestrator)

        assert cast(list[str], snapshot["running"]) == ["issue-1"]
        assert FakeCodexSession.max_active_runs == 1

        tracker.set_state("issue-1", "Done")
        release.set()
        await _wait_until(lambda: FakeCodexSession.stop_calls.get("ENG-1", 0) == 1)


@pytest.mark.anyio
async def test_bounded_concurrency_dispatches_only_two_at_a_time(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from symphony import issue_agent as issue_agent_module

    FakeCodexSession.reset()
    monkeypatch.setattr(issue_agent_module, "CodexSession", FakeCodexSession)

    workflow_path = tmp_path / "WORKFLOW.md"
    workspace_root = tmp_path / "workspaces"
    _write_workflow(
        workflow_path,
        workspace_root=workspace_root,
        max_concurrent_agents=2,
        per_state_limits={"todo": 2},
    )

    tracker = MemoryTracker()
    release_events: dict[int, anyio.Event] = {}
    base_time = datetime(2024, 1, 1, tzinfo=UTC)
    for number in range(1, 6):
        tracker.add_issue(_issue(number, created_at=base_time + timedelta(minutes=number)))
        release_events[number] = anyio.Event()
        FakeCodexSession.configure(
            f"ENG-{number}",
            scripts=["hold"],
            release_event=release_events[number],
        )

    async with Runtime():
        await _start_orchestrator(tracker=tracker, workflow_path=workflow_path)

        await _wait_until(lambda: sum(FakeCodexSession.run_calls.values()) == 2)
        assert FakeCodexSession.max_active_runs == 2

        tracker.set_state("issue-1", "Done")
        release_events[1].set()
        await _wait_until(lambda: sum(FakeCodexSession.run_calls.values()) == 3)

        tracker.set_state("issue-2", "Done")
        release_events[2].set()
        await _wait_until(lambda: sum(FakeCodexSession.run_calls.values()) == 4)

        tracker.set_state("issue-3", "Done")
        release_events[3].set()
        await _wait_until(lambda: sum(FakeCodexSession.run_calls.values()) == 5)

        tracker.set_state("issue-4", "Done")
        tracker.set_state("issue-5", "Done")
        release_events[4].set()
        release_events[5].set()
        await _wait_until(lambda: FakeCodexSession.active_runs == 0)

        assert sum(FakeCodexSession.run_calls.values()) == 5
        assert FakeCodexSession.max_active_runs == 2


@pytest.mark.anyio
async def test_crash_schedules_failure_retry_with_expected_attempt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from symphony import issue_agent as issue_agent_module
    from symphony import orchestrator as orchestrator_module

    FakeCodexSession.reset()
    monkeypatch.setattr(issue_agent_module, "CodexSession", FakeCodexSession)

    workflow_path = tmp_path / "WORKFLOW.md"
    workspace_root = tmp_path / "workspaces"
    _write_workflow(workflow_path, workspace_root=workspace_root)

    recorded: list[tuple[int, str]] = []
    real_retry_delay = orchestrator_module.retry_delay

    def fast_retry_delay(attempt: int, *, kind: str, settings: Settings) -> int:
        recorded.append((attempt, kind))
        del settings
        return 20

    monkeypatch.setattr(orchestrator_module, "retry_delay", fast_retry_delay)

    tracker = MemoryTracker()
    tracker.add_issue(_issue(1))
    FakeCodexSession.configure(
        "ENG-1",
        scripts=["crash", "return"],
        callbacks=[None, lambda: tracker.set_state("issue-1", "Done")],
    )

    settings = Settings.from_workflow_config(load(workflow_path).config)
    assert failure_retry_delay(1, settings) == FAILURE_RETRY_BASE_MS
    assert (
        real_retry_delay(1, kind="continuation", settings=settings)
        == CONTINUATION_RETRY_DELAY_MS
    )

    async with Runtime():
        await _start_orchestrator(tracker=tracker, workflow_path=workflow_path)
        await _wait_until(lambda: FakeCodexSession.run_calls.get("ENG-1", 0) >= 2)

    assert (1, "failure") in recorded


@pytest.mark.anyio
async def test_normal_exit_schedules_continuation_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from symphony import issue_agent as issue_agent_module
    from symphony import orchestrator as orchestrator_module

    FakeCodexSession.reset()
    monkeypatch.setattr(issue_agent_module, "CodexSession", FakeCodexSession)

    workflow_path = tmp_path / "WORKFLOW.md"
    workspace_root = tmp_path / "workspaces"
    _write_workflow(workflow_path, workspace_root=workspace_root)

    recorded: list[tuple[int, str]] = []

    def fast_retry_delay(attempt: int, *, kind: str, settings: Settings) -> int:
        recorded.append((attempt, kind))
        del settings
        return 20

    monkeypatch.setattr(orchestrator_module, "retry_delay", fast_retry_delay)

    tracker = MemoryTracker()
    tracker.add_issue(_issue(1))
    FakeCodexSession.configure(
        "ENG-1",
        scripts=["return", "return"],
        callbacks=[None, lambda: tracker.set_state("issue-1", "Done")],
    )

    async with Runtime():
        await _start_orchestrator(tracker=tracker, workflow_path=workflow_path)
        await _wait_until(lambda: FakeCodexSession.run_calls.get("ENG-1", 0) >= 2)

    assert (1, "continuation") in recorded


@pytest.mark.anyio
async def test_terminal_state_transition_stops_agent_and_cleans_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from symphony import issue_agent as issue_agent_module

    FakeCodexSession.reset()
    monkeypatch.setattr(issue_agent_module, "CodexSession", FakeCodexSession)

    workflow_path = tmp_path / "WORKFLOW.md"
    workspace_root = tmp_path / "workspaces"
    _write_workflow(workflow_path, workspace_root=workspace_root, poll_interval_ms=20)

    tracker = MemoryTracker()
    tracker.add_issue(_issue(1))
    release = anyio.Event()
    FakeCodexSession.configure("ENG-1", scripts=["hold"], release_event=release)

    workspace = workspace_root / "ENG-1"

    async with Runtime():
        await _start_orchestrator(tracker=tracker, workflow_path=workflow_path)
        await _wait_until(lambda: workspace.exists())
        await _wait_until(lambda: FakeCodexSession.run_calls.get("ENG-1", 0) == 1)

        tracker.set_state("issue-1", "Done")

        await _wait_until(lambda: not workspace.exists(), timeout=3.0)
        release.set()
        await _wait_until(lambda: FakeCodexSession.stop_calls.get("ENG-1", 0) == 1, timeout=5.0)
