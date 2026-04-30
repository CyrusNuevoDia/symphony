from __future__ import annotations

import json
import os
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_REFERENCE_RE = re.compile(r"^\$([A-Za-z_][A-Za-z0-9_]*)$")
_DEFAULT_APPROVAL_POLICY = {
    "reject": {"sandbox_approval": True, "rules": True, "mcp_elicitations": True}
}


def _resolve_secret(value: str | None, fallback_env_var: str) -> str | None:
    """Mirror of `resolve_secret_setting/2` from elixir/.../config/schema.ex.

    - None or empty           -> fallback env var (None if unset/empty)
    - "$NAME"                 -> NAME; unset -> fallback; "" -> None
    - any other literal       -> use as-is
    """

    def _env(name: str) -> str | None:
        raw = os.environ.get(name)
        return raw if raw not in (None, "") else None

    if not value:
        return _env(fallback_env_var)
    match = _ENV_REFERENCE_RE.match(value)
    if match is None:
        return value
    referenced = os.environ.get(match.group(1))
    if referenced is None:
        return _env(fallback_env_var)
    return None if referenced == "" else referenced


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
    api_key: str | None = None
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
        return _normalize_keys(_parse_jsonish(value))

    @field_validator("config_overrides", mode="before")
    @classmethod
    def _parse_overrides(cls, value: Any) -> Any:
        return _parse_string_list(value)


class _EnvOverlay(BaseSettings):
    """Reads SYMPHONY_<GROUP>__<FIELD> env vars into per-group dicts.

    Each group is `dict[str, Any]` so any inner key is collected without
    pre-declaring fields; per-field parsing happens in the matching `*Config`
    validators when the merged payload is validated.
    """

    model_config = SettingsConfigDict(
        env_prefix="SYMPHONY_",
        env_nested_delimiter="__",
        case_sensitive=False,
        extra="ignore",
    )

    tracker: dict[str, Any] = Field(default_factory=dict)
    polling: dict[str, Any] = Field(default_factory=dict)
    workspace: dict[str, Any] = Field(default_factory=dict)
    hooks: dict[str, Any] = Field(default_factory=dict)
    agent: dict[str, Any] = Field(default_factory=dict)
    codex: dict[str, Any] = Field(default_factory=dict)


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
            group: _normalize_keys(cfg.get(group, {}))
            for group in ("tracker", "polling", "workspace", "agent", "codex")
        }
        if "hooks" in cfg:
            payload["hooks"] = _normalize_keys(cfg.get("hooks"))
        env_patch = {k: v for k, v in _EnvOverlay().model_dump().items() if v}
        settings = cls.model_validate(_deep_merge(payload, env_patch))
        # Mirror elixir/.../config/schema.ex `finalize_settings/1`: resolve `$VAR` refs and
        # fall back to `LINEAR_API_KEY` when tracker.api_key is unset.
        settings.tracker.api_key = _resolve_secret(settings.tracker.api_key, "LINEAR_API_KEY")
        return settings
