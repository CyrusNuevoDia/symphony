from __future__ import annotations

from pathlib import Path

import anyio
import typer

from symphony.main import main

app = typer.Typer(
    help="Symphony orchestrates issue-driven Codex sessions.",
    no_args_is_help=True,
)


@app.command()
def run(
    workflow: Path = Path("WORKFLOW.md"),
    dry_run: bool = typer.Option(False, help="Use the in-memory tracker instead of Linear."),
) -> None:
    async def _run() -> None:
        await main(workflow_path=workflow, dry_run=dry_run)

    try:
        anyio.run(_run, backend="asyncio")
    except KeyboardInterrupt:
        return
