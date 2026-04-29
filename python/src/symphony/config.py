from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEFAULT_APPROVAL_POLICY = {
    "reject": {"sandbox_approval": True, "rules": True, "mcp_elicitations": True}
}


def _parse_jsonish(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text or text[0] not in "[{":
        return value
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return value


def _parse_string_list(value: Any) -> Any:
    value = _parse_jsonish(value)
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


def _normalize_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _normalize_keys(raw) for key, raw in value.items()}
    if isinstance(value, list):
        return [_normalize_keys(item) for item in value]
    return value


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


class TrackerConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    kind: Literal["linear", "memory"]
    project_slug: str | None = None
    active_states: list[str] = Field(default_factory=lambda: ["Todo", "In Progress"])
    terminal_states: list[str] = Field(
        default_factory=lambda: ["Closed", "Cancelled", "Canceled", "Duplicate", "Done"]
    )
    _parse_state_lists = field_validator(
        "active_states", "terminal_states", mode="before"
    )(_parse_string_list)


class PollingConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    interval_ms: int = 5000

    @field_validator("interval_ms")
    @classmethod
    def _validate_interval_ms(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("interval_ms must be greater than 0")
        return value


class WorkspaceConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    root: str = "~/code/symphony-workspaces"


class HooksConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    after_create: str | None = None
    before_remove: str | None = None


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    max_concurrent_agents: int = 1
    max_turns: int = 10
    max_concurrent_agents_by_state: dict[str, int] | None = None
    max_retry_backoff_ms: int = 600000

    @field_validator("max_concurrent_agents", "max_turns", "max_retry_backoff_ms")
    @classmethod
    def _validate_positive_int(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("value must be greater than 0")
        return value

    @field_validator("max_concurrent_agents_by_state", mode="before")
    @classmethod
    def _normalize_state_limits(cls, value: Any) -> Any:
        value = _parse_jsonish(value)
        if value is None:
            return None
        if not isinstance(value, dict):
            raise TypeError("max_concurrent_agents_by_state must be a mapping")
        normalized = {str(state).strip().lower(): limit for state, limit in value.items()}
        for state, limit in normalized.items():
            if not state:
                raise ValueError("state names must not be blank")
            if not isinstance(limit, int) or limit <= 0:
                raise ValueError("limits must be positive integers")
        return normalized


class CodexConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    command: list[str] | str = Field(default_factory=lambda: ["codex", "app-server"])
    approval_policy: str | dict[str, Any] = Field(default_factory=lambda: _DEFAULT_APPROVAL_POLICY)
    thread_sandbox: str = "workspace-write"
    turn_sandbox_policy: dict[str, Any] = Field(default_factory=dict)
    model: str | None = None
    config_overrides: list[str] | None = None

    @field_validator("command", mode="before")
    @classmethod
    def _parse_command(cls, value: Any) -> Any:
        return _parse_jsonish(value)

    @field_validator("approval_policy", "turn_sandbox_policy", mode="before")
    @classmethod
    def _normalize_mapping_fields(cls, value: Any) -> Any:
        value = _parse_jsonish(value)
        return _normalize_keys(value)

    @field_validator("config_overrides", mode="before")
    @classmethod
    def _parse_overrides(cls, value: Any) -> Any:
        return _parse_string_list(value)


class Settings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    tracker: TrackerConfig
    polling: PollingConfig
    workspace: WorkspaceConfig
    hooks: HooksConfig | None = None
    agent: AgentConfig
    codex: CodexConfig

    @classmethod
    def from_workflow_config(cls, cfg: dict[str, Any]) -> Settings:
        payload: dict[str, Any] = {
            "tracker": _normalize_keys(cfg.get("tracker", {})),
            "polling": _normalize_keys(cfg.get("polling", {})),
            "workspace": _normalize_keys(cfg.get("workspace", {})),
            "agent": _normalize_keys(cfg.get("agent", {})),
            "codex": _normalize_keys(cfg.get("codex", {})),
        }
        if "hooks" in cfg:
            payload["hooks"] = _normalize_keys(cfg.get("hooks"))
        return cls.model_validate(_deep_merge(payload, _EnvOverlay().to_settings_patch()))


class _EnvOverlay(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SYMPHONY_", extra="ignore", case_sensitive=False)

    tracker_kind: Literal["linear", "memory"] | None = None
    tracker_project_slug: str | None = None
    tracker_active_states: list[str] | None = None
    tracker_terminal_states: list[str] | None = None
    polling_interval_ms: int | None = None
    workspace_root: str | None = None
    hooks_after_create: str | None = None
    hooks_before_remove: str | None = None
    agent_max_concurrent_agents: int | None = None
    agent_max_turns: int | None = None
    agent_max_concurrent_agents_by_state: dict[str, int] | None = None
    agent_max_retry_backoff_ms: int | None = None
    codex_command: list[str] | str | None = None
    codex_approval_policy: str | dict[str, Any] | None = None
    codex_thread_sandbox: str | None = None
    codex_turn_sandbox_policy: dict[str, Any] | None = None
    codex_model: str | None = None
    codex_config_overrides: list[str] | None = None

    _parse_state_lists = field_validator(
        "tracker_active_states",
        "tracker_terminal_states",
        "codex_config_overrides",
        mode="before",
    )(_parse_string_list)
    _parse_command = field_validator("codex_command", mode="before")(_parse_jsonish)
    _parse_maps = field_validator(
        "agent_max_concurrent_agents_by_state",
        "codex_approval_policy",
        "codex_turn_sandbox_policy",
        mode="before",
    )(_parse_jsonish)

    def to_settings_patch(self) -> dict[str, Any]:
        patch: dict[str, Any] = {}
        tracker = {
            "kind": self.tracker_kind,
            "project_slug": self.tracker_project_slug,
            "active_states": self.tracker_active_states,
            "terminal_states": self.tracker_terminal_states,
        }
        polling = {"interval_ms": self.polling_interval_ms}
        workspace = {"root": self.workspace_root}
        hooks = {
            "after_create": self.hooks_after_create,
            "before_remove": self.hooks_before_remove,
        }
        agent = {
            "max_concurrent_agents": self.agent_max_concurrent_agents,
            "max_turns": self.agent_max_turns,
            "max_concurrent_agents_by_state": self.agent_max_concurrent_agents_by_state,
            "max_retry_backoff_ms": self.agent_max_retry_backoff_ms,
        }
        codex = {
            "command": self.codex_command,
            "approval_policy": _normalize_keys(self.codex_approval_policy),
            "thread_sandbox": self.codex_thread_sandbox,
            "turn_sandbox_policy": _normalize_keys(self.codex_turn_sandbox_policy),
            "model": self.codex_model,
            "config_overrides": self.codex_config_overrides,
        }
        for name, group in (
            ("tracker", tracker),
            ("polling", polling),
            ("workspace", workspace),
            ("hooks", hooks),
            ("agent", agent),
            ("codex", codex),
        ):
            cleaned = {key: value for key, value in group.items() if value is not None}
            if cleaned:
                patch[name] = cleaned
        return patch
