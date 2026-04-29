from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Protocol, cast

from codex_app_server_sdk import ConversationStep
from fastactor.otp import Continue, GenServer, Shutdown

from symphony.codex_session import CodexSession
from symphony.config import Settings
from symphony.logging import get_logger
from symphony.prompts import build_turn_prompt
from symphony.tracker import Issue
from symphony.workflow import Workflow
from symphony.workspace import Issue as WorkspaceIssue
from symphony.workspace import Settings as WorkspaceSettings
from symphony.workspace import ensure_worktree

logger = get_logger(__name__)


class ParentProcess(Protocol):
    def cast(self, request: object) -> None: ...

    async def call(self, request: object, timeout: float = 5.0) -> Issue: ...


class IssueAgent(GenServer):
    issue: Issue
    workflow: Workflow
    settings: Settings
    parent: ParentProcess
    workspace: Path
    session: CodexSession
    turn: int

    async def init(
        self,
        *,
        issue: Issue,
        workflow: Workflow,
        settings: Settings,
        parent: ParentProcess,
    ) -> Continue:
        self.issue = issue
        self.workflow = workflow
        self.settings = settings
        self.parent = parent
        self.turn = 0
        self.workspace = await ensure_worktree(
            cast(WorkspaceIssue, issue),
            cast(WorkspaceSettings, settings),
        )
        self.session = CodexSession(workspace=self.workspace, settings_codex=settings.codex)
        await self.session.start()
        return Continue("first_turn")

    async def handle_continue(self, term: object) -> Continue | None:
        if term not in {"first_turn", "next_turn"}:
            return None

        self.turn += 1
        prompt = build_turn_prompt(
            self.issue,
            self.workflow,
            attempt=self.turn if self.turn > 1 else None,
        )
        await self.session.run_turn(prompt, on_event=self._forward_event)
        if self.turn < self.settings.agent.max_turns and await self._should_continue():
            return Continue("next_turn")
        task = asyncio.create_task(self.stop(reason="normal"))
        task.add_done_callback(self._log_shutdown_task_error)
        return None

    async def terminate(self, reason: object) -> None:
        session = getattr(self, "session", None)
        if session is not None:
            try:
                await session.stop()
            except Exception:
                logger.exception(
                    "issue_agent.session_stop_failed",
                    issue_id=getattr(getattr(self, "issue", None), "id", None),
                    reason=reason,
                )
        await super().terminate(reason)

    def _forward_event(self, step: ConversationStep) -> None:
        try:
            self.parent.cast(("codex_event", self.issue.id, step))
        except Exception:
            logger.exception(
                "issue_agent.event_forward_failed",
                issue_id=self.issue.id,
                step_type=step.step_type,
            )

    async def _should_continue(self) -> bool:
        refreshed = await self.parent.call(("refresh_issue", self.issue.id), timeout=30.0)
        self.issue = refreshed
        active_states = {state.strip().lower() for state in self.settings.tracker.active_states}
        return refreshed.state.strip().lower() in active_states

    def _log_shutdown_task_error(self, task: asyncio.Task[None]) -> None:
        try:
            task.result()
        except Shutdown:
            return
        except Exception:
            logger.exception("issue_agent.shutdown_task_failed", issue_id=self.issue.id)
