from __future__ import annotations

import importlib
import os
import re
import subprocess
import sys
import types
from dataclasses import dataclass
from pathlib import Path

import pytest


@dataclass
class StubIssue:
    identifier: str
    id: str | None = "issue-1"
    title: str | None = "Example issue"
    state: str | None = "Todo"
    url: str | None = "https://linear.app/example/issue/1"


@dataclass
class StubHooksConfig:
    after_create: str | None = None
    before_remove: str | None = None
    timeout_ms: int = 5_000


@dataclass
class StubWorkspaceConfig:
    root: str


@dataclass
class StubSettings:
    workspace: StubWorkspaceConfig
    hooks: StubHooksConfig | None = None


def _git(repo: Path, *args: str) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Symphony Tests",
        "GIT_AUTHOR_EMAIL": "tests@example.com",
        "GIT_COMMITTER_NAME": "Symphony Tests",
        "GIT_COMMITTER_EMAIL": "tests@example.com",
    }
    subprocess.run(["git", *args], cwd=repo, check=True, env=env)


def _load_workspace_module(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    try:
        importlib.import_module("symphony.path_safety")
    except ImportError:
        module = types.ModuleType("symphony.path_safety")

        def canonicalize(path: str | Path) -> Path:
            return Path(path).expanduser().resolve(strict=False)

        def is_within(root: str | Path, candidate: str | Path) -> bool:
            root_path = canonicalize(root)
            candidate_path = canonicalize(candidate)
            try:
                candidate_path.relative_to(root_path)
            except ValueError:
                return False
            return candidate_path != root_path

        def safe_identifier(identifier: str | None) -> str:
            return re.sub(r"[^A-Za-z0-9._-]+", "_", identifier or "issue")

        module.__dict__["canonicalize"] = canonicalize
        module.__dict__["is_within"] = is_within
        module.__dict__["safe_identifier"] = safe_identifier
        monkeypatch.setitem(sys.modules, "symphony.path_safety", module)

    try:
        importlib.import_module("symphony.config")
    except ImportError:
        module = types.ModuleType("symphony.config")
        module.__dict__["Settings"] = StubSettings
        monkeypatch.setitem(sys.modules, "symphony.config", module)

    try:
        importlib.import_module("symphony.tracker")
    except ImportError:
        module = types.ModuleType("symphony.tracker")
        module.__dict__["Issue"] = StubIssue
        monkeypatch.setitem(sys.modules, "symphony.tracker", module)

    sys.modules.pop("symphony.workspace", None)
    return importlib.import_module("symphony.workspace")


@pytest.fixture
def workspace_module(monkeypatch: pytest.MonkeyPatch):
    return _load_workspace_module(monkeypatch)


@pytest.fixture
def parent_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "parent"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "README.md").write_text("workspace test\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")
    monkeypatch.chdir(repo)
    return repo


@pytest.mark.anyio
async def test_ensure_worktree_is_idempotent(
    workspace_module, parent_repo: Path, tmp_path: Path
) -> None:
    root = tmp_path / "workspaces"
    issue = StubIssue(identifier="ENG-123")
    settings = StubSettings(workspace=StubWorkspaceConfig(root=str(root)))

    workspace = await workspace_module.ensure_worktree(issue, settings)
    same_workspace = await workspace_module.ensure_worktree(issue, settings)

    assert workspace == same_workspace
    assert workspace.exists()
    assert root.resolve(strict=False) in workspace.resolve(strict=False).parents


@pytest.mark.anyio
async def test_cleanup_worktree_removes_workspace(
    workspace_module, parent_repo: Path, tmp_path: Path
) -> None:
    root = tmp_path / "workspaces"
    issue = StubIssue(identifier="ENG-124")
    settings = StubSettings(workspace=StubWorkspaceConfig(root=str(root)))

    workspace = await workspace_module.ensure_worktree(issue, settings)
    await workspace_module.cleanup_worktree(issue, settings)

    assert workspace.exists() is False


@pytest.mark.anyio
async def test_after_create_hook_creates_marker_file(
    workspace_module, parent_repo: Path, tmp_path: Path
) -> None:
    root = tmp_path / "workspaces"
    issue = StubIssue(identifier="ENG-125")
    hooks = StubHooksConfig(after_create="echo created > marker.txt")
    settings = StubSettings(workspace=StubWorkspaceConfig(root=str(root)), hooks=hooks)

    workspace = await workspace_module.ensure_worktree(issue, settings)

    assert (workspace / "marker.txt").read_text().strip() == "created"
