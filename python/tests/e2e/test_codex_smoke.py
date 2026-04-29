from __future__ import annotations

import os
from pathlib import Path

import pytest

from symphony.codex_session import CodexSession
from symphony.config import CodexConfig


@pytest.mark.anyio
@pytest.mark.skipif(not os.getenv("OPENAI_API_KEY"), reason="needs OPENAI_API_KEY")
async def test_codex_session_smoke(tmp_path: Path) -> None:
    session = CodexSession(
        workspace=tmp_path,
        settings_codex=CodexConfig(
            command=["codex", "app-server"],
            approval_policy="never",
            thread_sandbox="workspace-write",
            turn_sandbox_policy={"type": "workspaceWrite"},
        ),
    )

    await session.start()
    try:
        result = await session.run_turn("Reply with the word 'pong' and nothing else.")
    finally:
        await session.stop()

    assert session.thread_id is None
    assert result.final_text.strip()
