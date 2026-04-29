from __future__ import annotations

from pathlib import Path

import frontmatter

from symphony.config import Settings


def _workflow_fixture_path() -> Path:
    python_root = Path(__file__).resolve().parents[2]
    workflow_path = python_root / "WORKFLOW.md"
    if workflow_path.exists():
        return workflow_path
    return python_root.parent / "elixir" / "WORKFLOW.md"


def test_from_workflow_config_parses_real_workflow() -> None:
    metadata = frontmatter.load(str(_workflow_fixture_path())).metadata
    settings = Settings.from_workflow_config(metadata)

    assert settings.tracker.kind == "linear"
    assert settings.tracker.project_slug == "symphony-0c79b11b75ea"
    assert settings.polling.interval_ms == 5000
    assert settings.workspace.root == "~/code/symphony-workspaces"
    assert settings.agent.max_concurrent_agents == 10
    assert settings.agent.max_turns == 20
    assert "Done" in settings.tracker.terminal_states
    assert settings.hooks is not None
    assert settings.codex.approval_policy == "never"
    assert settings.codex.thread_sandbox == "workspace-write"
    assert settings.codex.turn_sandbox_policy == {"type": "workspaceWrite"}


def test_from_workflow_config_applies_env_overrides(monkeypatch) -> None:
    monkeypatch.setenv("SYMPHONY_TRACKER_KIND", "memory")
    monkeypatch.setenv("SYMPHONY_POLLING_INTERVAL_MS", "1500")
    monkeypatch.setenv("SYMPHONY_AGENT_MAX_CONCURRENT_AGENTS_BY_STATE", '{"In Progress": 2}')
    monkeypatch.setenv("SYMPHONY_CODEX_COMMAND", '["codex","serve"]')

    settings = Settings.from_workflow_config(
        {"tracker": {"kind": "linear"}, "polling": {}, "workspace": {}, "agent": {}, "codex": {}}
    )

    assert settings.tracker.kind == "memory"
    assert settings.polling.interval_ms == 1500
    assert settings.agent.max_concurrent_agents_by_state == {"in progress": 2}
    assert settings.codex.command == ["codex", "serve"]
    assert settings.hooks is None
