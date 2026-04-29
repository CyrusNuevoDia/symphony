from __future__ import annotations

from pathlib import Path

import pytest
from anyio import fail_after, sleep
from fastactor import Runtime

from symphony.workflow_store import WorkflowStore, current


def _write_workflow(path: Path, *, name: str, prompt: str) -> None:
    path.write_text(f"---\nname: {name}\n---\n{prompt}\n")


@pytest.mark.anyio
async def test_workflow_store_initial_load_returns_current_workflow(tmp_path: Path) -> None:
    workflow_path = tmp_path / "WORKFLOW.md"
    _write_workflow(workflow_path, name="alpha", prompt="First prompt")

    async with Runtime():
        store = await WorkflowStore.start_link(path=workflow_path, poll_interval_ms=50)

        loaded = await current(store)

        assert loaded.config == {"name": "alpha"}
        assert loaded.prompt_template == "First prompt"


@pytest.mark.anyio
async def test_workflow_store_reloads_after_poll_tick(tmp_path: Path) -> None:
    workflow_path = tmp_path / "WORKFLOW.md"
    _write_workflow(workflow_path, name="alpha", prompt="First prompt")

    async with Runtime():
        store = await WorkflowStore.start_link(path=workflow_path, poll_interval_ms=50)
        _write_workflow(workflow_path, name="beta", prompt="Updated prompt")

        with fail_after(0.25):
            while (await current(store)).prompt_template != "Updated prompt":
                await sleep(0.01)

        reloaded = await current(store)
        assert reloaded.config == {"name": "beta"}


@pytest.mark.anyio
async def test_workflow_store_force_reload_bypasses_timer(tmp_path: Path) -> None:
    workflow_path = tmp_path / "WORKFLOW.md"
    _write_workflow(workflow_path, name="alpha", prompt="First prompt")

    async with Runtime():
        store = await WorkflowStore.start_link(path=workflow_path, poll_interval_ms=60_000)
        _write_workflow(workflow_path, name="gamma", prompt="Reloaded immediately")

        store.cast("force_reload")
        reloaded = await current(store)

        assert reloaded.config == {"name": "gamma"}
        assert reloaded.prompt_template == "Reloaded immediately"
