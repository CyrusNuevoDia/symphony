from __future__ import annotations

from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import ClassVar

import anyio
import pytest
from codex_app_server_sdk import ChatResult, ConversationStep

from symphony.main import main
from symphony.tracker import Issue
from symphony.tracker.memory import MemoryTracker


class FakeCodexSession:
    callback_holder: ClassVar[list[Callable[[], None] | None]] = [None]
    run_calls: ClassVar[int] = 0
    start_calls: ClassVar[int] = 0
    stop_calls: ClassVar[int] = 0

    def __init__(self, *, workspace: Path | str, settings_codex: object) -> None:
        del settings_codex
        self.workspace = Path(workspace)

    @classmethod
    def reset(cls) -> None:
        cls.callback_holder = [None]
        cls.run_calls = 0
        cls.start_calls = 0
        cls.stop_calls = 0

    async def start(self) -> None:
        type(self).start_calls += 1

    async def run_turn(self, prompt: str, on_event=None) -> ChatResult:
        del prompt
        type(self).run_calls += 1
        await anyio.sleep(0)
        if on_event is not None:
            on_event(
                ConversationStep(
                    thread_id="thread-e2e",
                    turn_id="turn-e2e",
                    item_id="item-e2e",
                    step_type="codex",
                    item_type="agentMessage",
                    text="event-e2e",
                )
            )
        await anyio.sleep(0.01)
        callback = type(self).callback_holder[0]
        if callback is not None:
            callback()
        return ChatResult(
            thread_id="thread-e2e",
            turn_id="turn-e2e",
            final_text="done",
            raw_events=[],
            assistant_item_id="item-e2e",
            completion_source="item_completed",
        )

    async def stop(self) -> None:
        type(self).stop_calls += 1


async def _wait_until(
    predicate: Callable[[], bool],
    *,
    timeout: float = 5.0,
    interval: float = 0.02,
) -> None:
    with anyio.fail_after(timeout):
        while not predicate():
            await anyio.sleep(interval)


@pytest.mark.anyio
async def test_full_pipeline_reaches_terminal_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from symphony import issue_agent as issue_agent_module
    from symphony import orchestrator as orchestrator_module

    FakeCodexSession.reset()
    monkeypatch.setattr(issue_agent_module, "CodexSession", FakeCodexSession)
    monkeypatch.setattr(
        orchestrator_module,
        "retry_delay",
        lambda attempt, *, kind, settings: 20,
    )

    workspace_root = tmp_path / "workspaces"
    monkeypatch.setenv("SYMPHONY_WORKSPACE_ROOT", str(workspace_root))
    monkeypatch.setenv("SYMPHONY_HOOKS_AFTER_CREATE", ":")
    monkeypatch.setenv("SYMPHONY_HOOKS_BEFORE_REMOVE", ":")

    tracker = MemoryTracker()
    issue = Issue(
        id="issue-1",
        identifier="ENG-1",
        title="Pipeline issue",
        description="Run the full pipeline",
        state="Todo",
        url="https://example.com/issues/ENG-1",
    )
    tracker.add_issue(issue)
    FakeCodexSession.callback_holder[0] = lambda: tracker.set_state(issue.id, "Done")

    started = anyio.Event()
    workflow_path = Path(__file__).resolve().parents[2] / "WORKFLOW.md"
    workspace = workspace_root / issue.identifier

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(
            partial(
                main,
                workflow_path=workflow_path,
                dry_run=False,
                tracker=tracker,
                started=started,
            )
        )
        await started.wait()

        await _wait_until(lambda: workspace.exists())
        await _wait_until(lambda: FakeCodexSession.run_calls == 1)
        await _wait_until(lambda: not workspace.exists())
        await _wait_until(lambda: FakeCodexSession.stop_calls >= 1)
        await anyio.sleep(0.05)

        assert FakeCodexSession.start_calls == 1
        assert FakeCodexSession.run_calls == 1

        task_group.cancel_scope.cancel()
