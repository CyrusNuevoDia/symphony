from __future__ import annotations

import shlex
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from codex_app_server_sdk import (
    ApprovalPolicy,
    ChatResult,
    CodexClient,
    ConversationStep,
    ReasoningEffort,
    SandboxMode,
    ThreadConfig,
    ThreadHandle,
    TurnOverrides,
)
from codex_app_server_sdk.transport import StdioTransport

from symphony.config import CodexConfig
from symphony.logging import get_logger

logger = get_logger(__name__)

_APPROVAL_POLICIES = {"untrusted", "on-failure", "on-request", "never"}
_SANDBOX_MODES = {"read-only", "workspace-write", "danger-full-access"}
_REASONING_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh"}


class CodexSession:
    def __init__(self, *, workspace: Path | str, settings_codex: CodexConfig) -> None:
        self._workspace = Path(workspace)
        self._settings = settings_codex
        self._client: CodexClient | None = None
        self._thread: ThreadHandle | None = None
        self._thread_config = self._build_thread_config()
        self._turn_overrides = self._build_turn_overrides()

    async def start(self) -> None:
        if self._thread is not None:
            return
        client = CodexClient(StdioTransport(self._command(), cwd=str(self._workspace)))
        try:
            await client.__aenter__()
            thread = await client.start_thread(config=self._thread_config)
        except Exception:
            try:
                await client.__aexit__(None, None, None)
            except Exception:
                logger.exception("codex_session.start_cleanup_failed")
            raise
        self._client = client
        self._thread = thread

    async def run_turn(
        self,
        prompt: str,
        on_event: Callable[[ConversationStep], None] | None = None,
    ) -> ChatResult:
        thread = self._thread
        if thread is None:
            raise RuntimeError("CodexSession.start() must be called before run_turn()")

        assistant_step: ConversationStep | None = None
        last_text_step: ConversationStep | None = None
        async for step in thread.chat(prompt, turn_overrides=self._turn_overrides):
            if step.text:
                last_text_step = step
            if step.item_type == "agentMessage" and step.text:
                assistant_step = step
            if on_event is not None:
                try:
                    on_event(step)
                except Exception:
                    logger.exception(
                        "codex_session.event_callback_failed",
                        thread_id=thread.thread_id,
                        step_type=step.step_type,
                    )

        final_step = assistant_step or last_text_step
        if final_step is None or not final_step.text:
            raise RuntimeError("codex stream completed without a final text step")

        return ChatResult(
            thread_id=final_step.thread_id,
            turn_id=final_step.turn_id,
            final_text=final_step.text.strip(),
            raw_events=[],
            assistant_item_id=assistant_step.item_id if assistant_step is not None else None,
            completion_source="item_completed" if assistant_step is not None else None,
        )

    async def stop(self) -> None:
        client = self._client
        self._client = None
        self._thread = None
        if client is None:
            return
        try:
            await client.__aexit__(None, None, None)
        except Exception:
            logger.exception("codex_session.stop_failed")

    @property
    def thread_id(self) -> str | None:
        return self._thread.thread_id if self._thread is not None else None

    def _command(self) -> list[str]:
        command = self._settings.command
        argv = shlex.split(command) if isinstance(command, str) else list(command)
        if not argv:
            argv = ["codex"]
        overrides = [
            flag
            for item in self._settings.config_overrides or []
            for flag in ("-c", item)
        ]
        if "app-server" in argv:
            index = argv.index("app-server")
            return [*argv[:index], *overrides, *argv[index:]]
        return [*argv, *overrides, "app-server"]

    def _build_thread_config(self) -> ThreadConfig:
        kwargs: dict[str, Any] = {"cwd": str(self._workspace)}
        if self._settings.model:
            kwargs["model"] = self._settings.model
        if approval_policy := _approval_policy(self._settings.approval_policy):
            kwargs["approval_policy"] = approval_policy
        if sandbox := _sandbox_mode(self._settings.thread_sandbox):
            kwargs["sandbox"] = sandbox
        return ThreadConfig(**kwargs)

    def _build_turn_overrides(self) -> TurnOverrides | None:
        kwargs: dict[str, Any] = {}
        if self._settings.turn_sandbox_policy:
            kwargs["sandbox_policy"] = dict(self._settings.turn_sandbox_policy)
        reasoning_effort = getattr(self._settings, "reasoning_effort", None)
        if effort := _reasoning_effort(reasoning_effort):
            kwargs["effort"] = effort
        return TurnOverrides(**kwargs) if kwargs else None


def _approval_policy(value: object) -> ApprovalPolicy | None:
    if isinstance(value, str) and value in _APPROVAL_POLICIES:
        return cast(ApprovalPolicy, value)
    if value not in (None, {}):
        logger.warning("codex_session.unsupported_approval_policy", value=value)
    return None


def _sandbox_mode(value: object) -> SandboxMode | None:
    if isinstance(value, str) and value in _SANDBOX_MODES:
        return cast(SandboxMode, value)
    if value is not None:
        logger.warning("codex_session.unsupported_sandbox_mode", value=value)
    return None


def _reasoning_effort(value: object) -> ReasoningEffort | None:
    if isinstance(value, str) and value in _REASONING_EFFORTS:
        return cast(ReasoningEffort, value)
    if value is not None:
        logger.warning("codex_session.unsupported_reasoning_effort", value=value)
    return None
