from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import cast

import anyio
from anyio.abc import TaskGroup, TaskStatus
from fastactor.otp import Call, Cast, Down, DynamicSupervisor, GenServer, Info
from fastactor.otp.process import Process

from symphony.config import Settings
from symphony.issue_agent import IssueAgent
from symphony.logging import get_logger
from symphony.tracker import Issue, Tracker
from symphony.workflow import Workflow
from symphony.workflow_store import WorkflowStore
from symphony.workflow_store import current as current_workflow
from symphony.workspace import Issue as WorkspaceIssue
from symphony.workspace import Settings as WorkspaceSettings
from symphony.workspace import cleanup_worktree

logger = get_logger(__name__)

FAILURE_RETRY_BASE_MS = 10_000
CONTINUATION_RETRY_DELAY_MS = 1_000
TRACKER_TIMEOUT_S = 30.0
WORKFLOW_TIMEOUT_S = 5.0


def _monotonic_ms() -> int:
    return time.monotonic_ns() // 1_000_000


def _normalize_state(value: str | None) -> str:
    return value.strip().lower() if isinstance(value, str) else ""


def _priority_rank(priority: int | None) -> int:
    return priority if isinstance(priority, int) and priority in {1, 2, 3, 4} else 5


def _created_at_sort_key(issue: Issue) -> tuple[int, int]:
    created_at = issue.created_at
    if created_at is None:
        return (1, 0)
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    return (0, int(created_at.timestamp() * 1_000_000))


def _is_normal_shutdown_reason(reason: object) -> bool:
    return reason in {"normal", "shutdown"}


def failure_retry_delay(attempt: int, settings: Settings) -> int:
    # Port of `failure_retry_delay/1` from orchestrator.ex lines 936-938.
    cap = settings.agent.max_retry_backoff_ms
    return min(FAILURE_RETRY_BASE_MS * (1 << min(attempt - 1, 10)), cap)


def retry_delay(attempt: int, *, kind: str, settings: Settings) -> int:
    # Port of `retry_delay/2` from orchestrator.ex lines 928-932.
    if kind == "continuation" and attempt == 1:
        return CONTINUATION_RETRY_DELAY_MS
    return failure_retry_delay(attempt, settings)


@dataclass(slots=True)
class RetryEntry:
    issue_id: str
    attempt: int
    kind: str
    scheduled_at_ms: int
    cancel_scope: anyio.CancelScope | None = None


@dataclass(slots=True)
class RunningEntry:
    issue: Issue
    process: Process
    monitor_ref: str
    child_id: str
    attempt: int
    started_at: datetime


@dataclass(slots=True)
class State:
    settings: Settings
    tick_token: int = 0
    poll_in_progress: bool = False
    running: dict[str, RunningEntry] = field(default_factory=dict)
    claimed: set[str] = field(default_factory=set)
    completed: set[str] = field(default_factory=set)
    retry_attempts: dict[str, RetryEntry] = field(default_factory=dict)
    issue_state_cache: dict[str, str] = field(default_factory=dict)


class Orchestrator(GenServer[object, object]):
    tracker: Tracker
    workflow_store: WorkflowStore
    agents_sup: DynamicSupervisor
    registry: str
    state: State
    workflow: Workflow

    async def init(
        self,
        *,
        tracker: Tracker,
        workflow_store: WorkflowStore,
        agents_sup: DynamicSupervisor,
        registry: str,
        settings: Settings,
    ) -> None:
        self.tracker = tracker
        self.workflow_store = workflow_store
        self.agents_sup = agents_sup
        self.registry = registry
        self.state = State(settings=settings)
        self.workflow = Workflow(config={}, prompt_template="")
        self._tick_cancel_scope: anyio.CancelScope | None = None
        self._timer_group = anyio.create_task_group()
        await self._timer_group.__aenter__()
        self._next_tick_token = 0

        await self._refresh_runtime_config()
        await self._run_terminal_workspace_cleanup()
        await self._schedule_tick(0)

    async def handle_info(self, message: Info) -> None:
        match message:
            case Info(message=("tick", int() as token)):
                await self._handle_tick(token)
            case Info(message=("retry", str() as issue_id, int() as attempt)):
                await self._handle_retry(issue_id, attempt)
            case _:
                return None

    async def handle_down(self, message: Down) -> None:
        await self._handle_agent_down(message)

    async def handle_call(self, call: Call[object, object]) -> object:
        match call.message:
            case ("refresh_issue", str() as issue_id):
                return await self._refresh_issue_for_agent(issue_id)
            case "snapshot":
                return {
                    "tick_token": self.state.tick_token,
                    "poll_in_progress": self.state.poll_in_progress,
                    "running": sorted(self.state.running),
                    "claimed": sorted(self.state.claimed),
                    "completed": sorted(self.state.completed),
                    "retry_attempts": {
                        issue_id: {
                            "attempt": entry.attempt,
                            "kind": entry.kind,
                            "scheduled_at_ms": entry.scheduled_at_ms,
                        }
                        for issue_id, entry in self.state.retry_attempts.items()
                    },
                    "issue_state_cache": dict(self.state.issue_state_cache),
                }
            case _:
                raise ValueError(f"unsupported orchestrator call: {call.message!r}")

    async def handle_cast(self, cast: Cast[object]) -> None:
        match cast.message:
            case ("codex_event", str() as issue_id, step):
                logger.info(
                    "orchestrator.codex_event",
                    issue_id=issue_id,
                    step_type=getattr(step, "step_type", None),
                    item_type=getattr(step, "item_type", None),
                )
            case _:
                return None

    async def on_terminate(self, reason: object) -> None:
        self._cancel_tick()
        for issue_id in list(self.state.retry_attempts):
            self._cancel_retry(issue_id)
        for issue_id in list(self.state.running):
            await self._terminate_running_issue(issue_id, cleanup_workspace=False)
        timer_group: TaskGroup | None = getattr(self, "_timer_group", None)
        if timer_group is not None:
            self._timer_group = None
            timer_group.cancel_scope.cancel()
            await timer_group.__aexit__(None, None, None)
        logger.info("orchestrator.terminated", reason=reason)

    async def _handle_tick(self, token: int) -> None:
        # Port of the stale tick-token drop from orchestrator.ex lines 74-117.
        if token != self.state.tick_token:
            return

        self.state.tick_token = 0
        self.state.poll_in_progress = True
        self._tick_cancel_scope = None

        try:
            await self._refresh_runtime_config()
            await self._poll_and_dispatch()
        except Exception:
            logger.exception("orchestrator.poll_failed")
        finally:
            self.state.poll_in_progress = False
            await self._schedule_tick(self.state.settings.polling.interval_ms)

    async def _handle_retry(self, issue_id: str, attempt: int) -> None:
        retry_entry = self.state.retry_attempts.get(issue_id)
        if retry_entry is None or retry_entry.attempt != attempt:
            return

        self.state.retry_attempts.pop(issue_id, None)
        try:
            await self._dispatch_one(issue_id, attempt, kind=retry_entry.kind)
        except Exception:
            logger.exception(
                "orchestrator.retry_failed",
                issue_id=issue_id,
                attempt=attempt,
                kind=retry_entry.kind,
            )
            await self._schedule_retry(issue_id, attempt + 1, kind=retry_entry.kind)

    async def _handle_agent_down(self, down: Down) -> None:
        issue_id = self._find_issue_id_for_ref(down.ref)
        if issue_id is None:
            return

        running_entry = self.state.running.pop(issue_id)
        self.state.claimed.discard(issue_id)
        self._delete_child_spec(issue_id)

        if _is_normal_shutdown_reason(down.reason):
            self.state.completed.add(issue_id)
            await self._schedule_retry(issue_id, 1, kind="continuation")
            return

        self.state.completed.discard(issue_id)
        next_attempt = running_entry.attempt + 1 if running_entry.attempt > 0 else 1
        await self._schedule_retry(issue_id, next_attempt, kind="failure")

    async def _poll_and_dispatch(self) -> None:
        await self._reconcile_running_issues()
        if self._available_slots() <= 0:
            return

        with anyio.fail_after(TRACKER_TIMEOUT_S):
            issues = await self.tracker.fetch_candidate_issues()
        for issue in issues:
            self.state.issue_state_cache[issue.id] = issue.state

        # Preserve the Elixir dispatch sort from orchestrator.ex lines 224-273.
        for issue in sorted(
            issues,
            key=lambda item: (
                _priority_rank(item.priority),
                _created_at_sort_key(item),
                item.identifier,
            ),
        ):
            if not self._should_dispatch_issue(issue):
                continue
            started = await self._dispatch_issue(issue, attempt=0)
            if started and self._available_slots() <= 0:
                break

    async def _refresh_runtime_config(self) -> None:
        try:
            with anyio.fail_after(WORKFLOW_TIMEOUT_S):
                workflow = await current_workflow(self.workflow_store)
            settings = Settings.from_workflow_config(workflow.config)
        except Exception:
            logger.exception("orchestrator.workflow_refresh_failed")
            return

        self.workflow = workflow
        self.state.settings = settings
        self.agents_sup.max_children = settings.agent.max_concurrent_agents

    async def _run_terminal_workspace_cleanup(self) -> None:
        try:
            with anyio.fail_after(TRACKER_TIMEOUT_S):
                issues = await self.tracker.fetch_issues_by_states(
                    self.state.settings.tracker.terminal_states
                )
        except Exception:
            logger.exception("orchestrator.startup_cleanup_failed")
            return

        for issue in issues:
            self.state.issue_state_cache[issue.id] = issue.state
            with contextlib.suppress(Exception):
                await cleanup_worktree(
                    cast(WorkspaceIssue, issue),
                    cast(WorkspaceSettings, self.state.settings),
                )

    async def _reconcile_running_issues(self) -> None:
        issue_ids = list(self.state.running)
        if not issue_ids:
            return

        try:
            with anyio.fail_after(TRACKER_TIMEOUT_S):
                refreshed = await self.tracker.fetch_issue_states_by_ids(issue_ids)
        except Exception:
            logger.exception("orchestrator.running_refresh_failed")
            return

        by_id = {issue.id: issue for issue in refreshed}
        active_states = self._active_state_set()
        terminal_states = self._terminal_state_set()

        for issue_id in issue_ids:
            issue = by_id.get(issue_id)
            if issue is None:
                logger.info("orchestrator.running_issue_missing", issue_id=issue_id)
                await self._terminate_running_issue(issue_id, cleanup_workspace=False)
                continue

            self.state.issue_state_cache[issue_id] = issue.state
            self.state.running[issue_id].issue = issue
            normalized_state = _normalize_state(issue.state)

            if normalized_state in terminal_states:
                logger.info(
                    "orchestrator.running_issue_terminal",
                    issue_id=issue.id,
                    identifier=issue.identifier,
                    state=issue.state,
                )
                await self._terminate_running_issue(issue_id, cleanup_workspace=True)
            elif normalized_state not in active_states:
                logger.info(
                    "orchestrator.running_issue_inactive",
                    issue_id=issue.id,
                    identifier=issue.identifier,
                    state=issue.state,
                )
                await self._terminate_running_issue(issue_id, cleanup_workspace=False)

    async def _refresh_issue_for_agent(self, issue_id: str) -> Issue:
        try:
            issue = await self._fetch_issue_by_id(issue_id)
        except Exception:
            issue = None
        if issue is not None:
            return issue
        if running_entry := self.state.running.get(issue_id):
            return running_entry.issue
        raise LookupError(f"unable to refresh issue {issue_id!r}")

    async def _fetch_issue_by_id(self, issue_id: str) -> Issue | None:
        with anyio.fail_after(TRACKER_TIMEOUT_S):
            issues = await self.tracker.fetch_issue_states_by_ids([issue_id])
        return issues[0] if issues else None

    async def _dispatch_one(self, issue_id: str, attempt: int, *, kind: str) -> None:
        issue = await self._fetch_issue_by_id(issue_id)
        if issue is None:
            self._clear_issue_tracking(issue_id)
            return

        self.state.issue_state_cache[issue_id] = issue.state
        normalized_state = _normalize_state(issue.state)
        if normalized_state in self._terminal_state_set():
            with contextlib.suppress(Exception):
                await cleanup_worktree(
                    cast(WorkspaceIssue, issue),
                    cast(WorkspaceSettings, self.state.settings),
                )
            self._clear_issue_tracking(issue_id)
            return

        if normalized_state not in self._active_state_set():
            self._clear_issue_tracking(issue_id)
            return

        if not self._dispatch_slots_available(issue):
            await self._schedule_retry(issue_id, attempt + 1, kind=kind)
            return

        started = await self._dispatch_issue(issue, attempt=attempt)
        if not started:
            await self._schedule_retry(issue_id, attempt + 1, kind=kind)

    async def _dispatch_issue(self, issue: Issue, *, attempt: int) -> bool:
        if issue.id in self.state.running:
            return False

        refreshed_issue = await self._fetch_issue_by_id(issue.id)
        if refreshed_issue is None:
            self._clear_issue_tracking(issue.id)
            return False

        self.state.issue_state_cache[issue.id] = refreshed_issue.state
        if not self._is_retry_candidate(refreshed_issue):
            self._clear_issue_tracking(issue.id)
            return False

        if not self._dispatch_slots_available(refreshed_issue):
            return False

        self._delete_child_spec(issue.id)
        child_spec = DynamicSupervisor.child_spec(
            issue.id,
            IssueAgent,
            kwargs={
                "issue": refreshed_issue,
                "workflow": self.workflow,
                "settings": self.state.settings,
                "parent": self,
                "via": (self.registry, refreshed_issue.id),
            },
            # The orchestrator owns retry/backoff semantics; auto-restarting children would bypass
            # the Elixir retry flow that this port preserves.
            restart="temporary",
        )

        try:
            process = await self.agents_sup.start_child(child_spec)
        except Exception:
            logger.exception(
                "orchestrator.dispatch_failed",
                issue_id=refreshed_issue.id,
                identifier=refreshed_issue.identifier,
                attempt=attempt,
            )
            return False

        if process.has_stopped():
            self._delete_child_spec(issue.id)
            reason = process._crash_exc or "normal"
            if _is_normal_shutdown_reason(reason):
                self.state.completed.add(issue.id)
                await self._schedule_retry(issue.id, 1, kind="continuation")
            else:
                next_attempt = attempt + 1 if attempt > 0 else 1
                await self._schedule_retry(issue.id, next_attempt, kind="failure")
            return False

        monitor_ref = self.monitor(process)
        self.state.running[refreshed_issue.id] = RunningEntry(
            issue=refreshed_issue,
            process=process,
            monitor_ref=monitor_ref,
            child_id=refreshed_issue.id,
            attempt=attempt,
            started_at=datetime.now(UTC),
        )
        self.state.claimed.add(refreshed_issue.id)
        self.state.completed.discard(refreshed_issue.id)
        self._cancel_retry(refreshed_issue.id)
        logger.info(
            "orchestrator.dispatched",
            issue_id=refreshed_issue.id,
            identifier=refreshed_issue.identifier,
            attempt=attempt,
        )
        return True

    async def _terminate_running_issue(self, issue_id: str, *, cleanup_workspace: bool) -> None:
        running_entry = self.state.running.pop(issue_id, None)
        self.state.claimed.discard(issue_id)
        self.state.completed.discard(issue_id)
        self._cancel_retry(issue_id)

        if running_entry is None:
            self._delete_child_spec(issue_id)
            return

        if cleanup_workspace:
            with contextlib.suppress(Exception):
                await cleanup_worktree(
                    cast(WorkspaceIssue, running_entry.issue),
                    cast(WorkspaceSettings, self.state.settings),
                )

        with contextlib.suppress(Exception):
            await self.agents_sup.terminate_child(running_entry.child_id)
        self._delete_child_spec(issue_id)

    async def _schedule_tick(self, delay_ms: int) -> None:
        self._cancel_tick()
        self._next_tick_token += 1
        self.state.tick_token = self._next_tick_token
        self._tick_cancel_scope = await self._schedule_info_after(
            delay_ms,
            ("tick", self._next_tick_token),
        )

    async def _schedule_retry(self, issue_id: str, attempt: int, *, kind: str) -> None:
        self._cancel_retry(issue_id)
        delay_ms = retry_delay(attempt, kind=kind, settings=self.state.settings)
        cancel_scope = await self._schedule_info_after(delay_ms, ("retry", issue_id, attempt))
        self.state.retry_attempts[issue_id] = RetryEntry(
            issue_id=issue_id,
            attempt=attempt,
            kind=kind,
            scheduled_at_ms=_monotonic_ms() + delay_ms,
            cancel_scope=cancel_scope,
        )

    async def _schedule_info_after(
        self,
        delay_ms: int,
        payload: object,
    ) -> anyio.CancelScope:
        timer_group = self._timer_group
        if timer_group is None:
            raise RuntimeError("orchestrator timer group is not running")
        return await timer_group.start(self._timer_task, delay_ms, payload)

    async def _timer_task(
        self,
        delay_ms: int,
        payload: object,
        *,
        task_status: TaskStatus[anyio.CancelScope],
    ) -> None:
        with anyio.CancelScope() as cancel_scope:
            task_status.started(cancel_scope)
            with anyio.move_on_after(max(delay_ms, 0) / 1000) as timeout_scope:
                await anyio.sleep_forever()
            if timeout_scope.cancelled_caught and not cancel_scope.cancel_called:
                self.info(payload, sender=self)

    def _cancel_tick(self) -> None:
        if self._tick_cancel_scope is None:
            return
        self._tick_cancel_scope.cancel()
        self._tick_cancel_scope = None

    def _cancel_retry(self, issue_id: str) -> None:
        retry_entry = self.state.retry_attempts.pop(issue_id, None)
        if retry_entry is not None and retry_entry.cancel_scope is not None:
            retry_entry.cancel_scope.cancel()

    def _delete_child_spec(self, issue_id: str) -> None:
        # fastactor keeps completed child specs until they are explicitly deleted.
        with contextlib.suppress(Exception):
            self.agents_sup.delete_child(issue_id)

    def _find_issue_id_for_ref(self, monitor_ref: str | None) -> str | None:
        if monitor_ref is None:
            return None
        for issue_id, running_entry in self.state.running.items():
            if running_entry.monitor_ref == monitor_ref:
                return issue_id
        return None

    def _active_state_set(self) -> set[str]:
        return {
            _normalize_state(state)
            for state in self.state.settings.tracker.active_states
            if _normalize_state(state)
        }

    def _terminal_state_set(self) -> set[str]:
        return {
            _normalize_state(state)
            for state in self.state.settings.tracker.terminal_states
            if _normalize_state(state)
        }

    def _available_slots(self) -> int:
        limit = self.state.settings.agent.max_concurrent_agents
        return max(limit - len(self.state.running), 0)

    def _dispatch_slots_available(self, issue: Issue) -> bool:
        return self._available_slots() > 0 and self._state_slots_available(issue)

    def _state_slots_available(self, issue: Issue) -> bool:
        normalized_state = _normalize_state(issue.state)
        per_state = self.state.settings.agent.max_concurrent_agents_by_state or {}
        limit = per_state.get(
            normalized_state,
            self.state.settings.agent.max_concurrent_agents,
        )
        used = sum(
            1
            for running_entry in self.state.running.values()
            if _normalize_state(running_entry.issue.state) == normalized_state
        )
        return used < limit

    def _should_dispatch_issue(self, issue: Issue) -> bool:
        issue_id = issue.id
        normalized_state = _normalize_state(issue.state)
        active_states = self._active_state_set()
        terminal_states = self._terminal_state_set()
        if not issue.assigned_to_worker:
            return False
        if normalized_state not in active_states:
            return False
        if normalized_state in terminal_states:
            return False
        if issue_id in self.state.claimed:
            return False
        if issue_id in self.state.running:
            return False
        if issue_id in self.state.completed:
            return False
        if issue_id in self.state.retry_attempts:
            return False
        return self._dispatch_slots_available(issue)

    def _is_retry_candidate(self, issue: Issue) -> bool:
        normalized_state = _normalize_state(issue.state)
        return (
            issue.assigned_to_worker
            and normalized_state in self._active_state_set()
            and normalized_state not in self._terminal_state_set()
        )

    def _clear_issue_tracking(self, issue_id: str) -> None:
        self.state.claimed.discard(issue_id)
        self.state.completed.discard(issue_id)
        self._cancel_retry(issue_id)
        self.state.issue_state_cache.pop(issue_id, None)
