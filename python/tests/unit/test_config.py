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
    monkeypatch.setenv("SYMPHONY_TRACKER__KIND", "memory")
    monkeypatch.setenv("SYMPHONY_POLLING__INTERVAL_MS", "1500")
    monkeypatch.setenv("SYMPHONY_AGENT__MAX_CONCURRENT_AGENTS_BY_STATE", '{"In Progress": 2}')
    monkeypatch.setenv("SYMPHONY_CODEX__COMMAND", '["codex","serve"]')

    settings = Settings.from_workflow_config(
        {"tracker": {"kind": "linear"}, "polling": {}, "workspace": {}, "agent": {}, "codex": {}}
    )

    assert settings.tracker.kind == "memory"
    assert settings.polling.interval_ms == 1500
    assert settings.agent.max_concurrent_agents_by_state == {"in progress": 2}
    assert settings.codex.command == ["codex", "serve"]
    assert settings.hooks is None


def _linear_cfg(tracker_extras: dict[str, str] | None = None) -> dict:
    tracker: dict = {"kind": "linear"}
    if tracker_extras:
        tracker.update(tracker_extras)
    return {"tracker": tracker, "polling": {}, "workspace": {}, "agent": {}, "codex": {}}


def test_tracker_api_key_falls_back_to_linear_api_key_env(monkeypatch) -> None:
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    monkeypatch.setenv("LINEAR_API_KEY", "env-token")

    settings = Settings.from_workflow_config(_linear_cfg())

    assert settings.tracker.api_key == "env-token"


def test_tracker_api_key_resolves_dollar_reference(monkeypatch) -> None:
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    monkeypatch.setenv("CUSTOM_LINEAR_TOKEN", "ref-token")

    settings = Settings.from_workflow_config(
        _linear_cfg({"api_key": "$CUSTOM_LINEAR_TOKEN"})
    )

    assert settings.tracker.api_key == "ref-token"


def test_tracker_api_key_literal_passes_through(monkeypatch) -> None:
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)

    settings = Settings.from_workflow_config(_linear_cfg({"api_key": "lin_api_literal"}))

    assert settings.tracker.api_key == "lin_api_literal"


def test_tracker_api_key_unset_when_neither_workflow_nor_env(monkeypatch) -> None:
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)

    settings = Settings.from_workflow_config(_linear_cfg())

    assert settings.tracker.api_key is None


def test_tracker_api_key_dollar_reference_falls_back_when_target_unset(monkeypatch) -> None:
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    monkeypatch.setenv("LINEAR_API_KEY", "fallback-token")
    monkeypatch.delenv("MISSING_VAR", raising=False)

    settings = Settings.from_workflow_config(_linear_cfg({"api_key": "$MISSING_VAR"}))

    assert settings.tracker.api_key == "fallback-token"
