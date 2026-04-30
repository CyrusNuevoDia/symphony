from __future__ import annotations

from collections.abc import Awaitable
from pathlib import Path
from typing import cast

import anyio
from fastactor import Runtime
from fastactor.otp import DynamicSupervisor, Registry

from symphony.config import Settings
from symphony.logging import configure_logging
from symphony.orchestrator import Orchestrator
from symphony.tracker import Tracker
from symphony.tracker.linear import LinearTracker
from symphony.tracker.memory import MemoryTracker
from symphony.workflow import load
from symphony.workflow_store import WorkflowStore

REGISTRY_NAME = "symphony.agents"


def build_tracker(
    settings: Settings,
    *,
    dry_run: bool = False,
    tracker: Tracker | None = None,
) -> Tracker:
    if tracker is not None:
        return tracker
    if dry_run or settings.tracker.kind == "memory":
        return MemoryTracker()
    if settings.tracker.kind != "linear":
        raise ValueError(f"unsupported tracker kind: {settings.tracker.kind!r}")
    api_key = settings.tracker.api_key
    if not api_key:
        raise RuntimeError(
            "tracker.api_key is required for the Linear tracker "
            "(set LINEAR_API_KEY in the environment or `tracker.api_key` in WORKFLOW.md)"
        )
    project_slug = settings.tracker.project_slug
    if not project_slug:
        raise RuntimeError("tracker.project_slug is required for the Linear tracker")
    return LinearTracker(
        api_key=api_key,
        project_slug=project_slug,
        active_states=settings.tracker.active_states,
        terminal_states=settings.tracker.terminal_states,
    )


async def main(
    *,
    workflow_path: Path,
    dry_run: bool = False,
    tracker: Tracker | None = None,
    started: anyio.Event | None = None,
) -> None:
    configure_logging()
    workflow = load(workflow_path)
    settings = Settings.from_workflow_config(workflow.config)
    tracker_instance = build_tracker(settings, dry_run=dry_run, tracker=tracker)

    try:
        async with Runtime():
            await Registry.new(REGISTRY_NAME, "unique")
            agents_sup = await DynamicSupervisor.start_link(
                name="symphony.agents_sup",
                max_children=settings.agent.max_concurrent_agents,
            )
            workflow_store = await WorkflowStore.start_link(
                name="symphony.workflow_store",
                path=workflow_path,
                poll_interval_ms=1_000,
            )
            await Orchestrator.start_link(
                name="symphony.orchestrator",
                tracker=tracker_instance,
                workflow_store=workflow_store,
                agents_sup=agents_sup,
                registry=REGISTRY_NAME,
                settings=settings,
            )
            if started is not None:
                started.set()
            await anyio.sleep_forever()
    finally:
        aclose = getattr(tracker_instance, "aclose", None)
        if callable(aclose):
            await cast(Awaitable[None], aclose())
