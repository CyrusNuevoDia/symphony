from __future__ import annotations

import logging
from pathlib import Path

import anyio
from anyio.abc import TaskGroup
from fastactor.otp import Call, Cast, GenServer, Info

from . import workflow as workflow_loader
from .workflow import Workflow

logger = logging.getLogger(__name__)


class WorkflowStore(GenServer[str, Workflow]):
    async def init(self, *, path: Path, poll_interval_ms: int = 1000) -> None:
        self._path = path
        self._poll_interval_s = poll_interval_ms / 1000
        self._workflow = workflow_loader.load(path)
        self._stamp = workflow_loader.stamp(path)
        self._poll_group = anyio.create_task_group()
        await self._poll_group.__aenter__()
        self._poll_group.start_soon(self._poll_loop)

    async def handle_call(self, call: Call[str, Workflow]) -> Workflow:
        match call.message:
            case "current":
                return self._workflow
            case _:
                raise ValueError(f"unsupported WorkflowStore call: {call.message!r}")

    async def handle_cast(self, cast: Cast[str]) -> None:
        match cast.message:
            case "force_reload":
                await self._reload(force=True)
            case _:
                raise ValueError(f"unsupported WorkflowStore cast: {cast.message!r}")

    async def handle_info(self, message: Info) -> None:
        if message.message == "poll":
            await self._reload(force=False)

    async def on_terminate(self, reason: object) -> None:
        del reason
        poll_group: TaskGroup | None = getattr(self, "_poll_group", None)
        if poll_group is None:
            return
        self._poll_group = None
        poll_group.cancel_scope.cancel()
        await poll_group.__aexit__(None, None, None)

    async def _poll_loop(self) -> None:
        while True:
            await anyio.sleep(self._poll_interval_s)
            self.info("poll", sender=self)

    async def _reload(self, *, force: bool) -> None:
        try:
            if not force:
                stamp = workflow_loader.stamp(self._path)
                if stamp == self._stamp:
                    return
            next_workflow = workflow_loader.load(self._path)
            next_stamp = workflow_loader.stamp(self._path)
        except Exception:
            logger.exception(
                "failed to reload workflow %s; keeping last known good version",
                self._path,
            )
            return

        self._workflow = next_workflow
        self._stamp = next_stamp


async def current(store_pid: WorkflowStore) -> Workflow:
    return await store_pid.call("current")
