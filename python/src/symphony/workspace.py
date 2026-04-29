from __future__ import annotations

import asyncio
import os
import re
import shutil
from pathlib import Path
from typing import Protocol

from .logging import get_logger

try:
    from symphony import path_safety as _path_safety
except ImportError:
    class _PathSafetyFallback:
        @staticmethod
        def canonicalize(path: str | Path) -> Path:
            return Path(path).expanduser().resolve(strict=False)

        @staticmethod
        def is_within(root: str | Path, candidate: str | Path) -> bool:
            root_path = _PathSafetyFallback.canonicalize(root)
            candidate_path = _PathSafetyFallback.canonicalize(candidate)
            try:
                candidate_path.relative_to(root_path)
            except ValueError:
                return False
            return candidate_path != root_path

        @staticmethod
        def safe_identifier(identifier: str) -> str:
            return re.sub(r"[^A-Za-z0-9._-]+", "_", identifier)

    _path_safety = _PathSafetyFallback()


logger = get_logger(__name__)


class Issue(Protocol):
    identifier: str
    id: str | None
    title: str | None
    state: str | None
    url: str | None


class HooksConfig(Protocol):
    after_create: str | None
    before_remove: str | None
    timeout_ms: int


class _WorkspaceConfig(Protocol):
    root: str


class Settings(Protocol):
    workspace: _WorkspaceConfig
    hooks: HooksConfig | None


async def ensure_worktree(issue: Issue, settings: Settings) -> Path:
    workspace = _workspace_path(issue, settings)
    if await _is_git_worktree(workspace):
        return workspace

    created_now = False
    if _cwd_has_git_dir():
        workspace.parent.mkdir(parents=True, exist_ok=True)
        if workspace.exists():
            if workspace.is_dir() and not any(workspace.iterdir()):
                workspace.rmdir()
            else:
                raise FileExistsError(f"workspace path already exists: {workspace}")
        await _run_command("git", "worktree", "add", str(workspace), cwd=Path.cwd())
        created_now = True
    else:
        created_now = not workspace.exists()
        workspace.mkdir(parents=True, exist_ok=True)

    if created_now:
        try:
            await run_after_create_hook(workspace, issue, getattr(settings, "hooks", None))
        except Exception:
            await _discard_workspace(workspace)
            raise
    return workspace


async def cleanup_worktree(issue: Issue, settings: Settings) -> None:
    workspace = _workspace_path(issue, settings)
    if not workspace.exists():
        return

    try:
        await run_before_remove_hook(workspace, issue, getattr(settings, "hooks", None))
    except Exception as exc:
        logger.warning(
            "workspace.before_remove_hook_failed",
            workspace=str(workspace),
            issue_identifier=issue.identifier,
            error=str(exc),
        )

    if await _is_git_worktree(workspace):
        await _run_command("git", "worktree", "remove", "--force", str(workspace), cwd=Path.cwd())
    elif workspace.is_dir():
        shutil.rmtree(workspace)
    else:
        workspace.unlink()


async def run_after_create_hook(
    workspace: Path, issue: Issue, hooks: HooksConfig | None
) -> None:
    command = getattr(hooks, "after_create", None)
    if command is None:
        return
    await _run_hook("after_create", command, workspace, issue, hooks)


async def run_before_remove_hook(
    workspace: Path, issue: Issue, hooks: HooksConfig | None
) -> None:
    command = getattr(hooks, "before_remove", None)
    if command is None:
        return
    await _run_hook("before_remove", command, workspace, issue, hooks)


def _workspace_path(issue: Issue, settings: Settings) -> Path:
    root = Path(settings.workspace.root).expanduser()
    workspace = root / _path_safety.safe_identifier(issue.identifier)
    if not _path_safety.is_within(
        Path(_path_safety.canonicalize(root)),
        Path(_path_safety.canonicalize(workspace)),
    ):
        raise ValueError(f"workspace path escapes root: {workspace}")
    return workspace


def _cwd_has_git_dir() -> bool:
    return (Path.cwd() / ".git").exists()


async def _is_git_worktree(workspace: Path) -> bool:
    if not workspace.is_dir() or not (workspace / ".git").is_file():
        return False
    try:
        stdout, _ = await _run_command(
            "git", "-C", str(workspace), "rev-parse", "--is-inside-work-tree", cwd=Path.cwd()
        )
    except RuntimeError:
        return False
    return stdout.strip() == "true"


async def _discard_workspace(workspace: Path) -> None:
    if not workspace.exists():
        return
    if await _is_git_worktree(workspace) and _cwd_has_git_dir():
        await _run_command("git", "worktree", "remove", "--force", str(workspace), cwd=Path.cwd())
        return
    if workspace.is_dir():
        shutil.rmtree(workspace)
    else:
        workspace.unlink()


async def _run_command(*args: str, cwd: Path) -> tuple[str, str]:
    process = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await process.communicate()
    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")
    if process.returncode != 0:
        raise RuntimeError(stderr.strip() or stdout.strip() or f"command failed: {' '.join(args)}")
    return stdout, stderr


async def _run_hook(
    hook_name: str, command: str, workspace: Path, issue: Issue, hooks: HooksConfig | None
) -> None:
    timeout_ms = getattr(hooks, "timeout_ms", 60_000)
    logger.info(
        "workspace.hook.started",
        hook=hook_name,
        workspace=str(workspace),
        issue_identifier=issue.identifier,
    )
    process = await asyncio.create_subprocess_shell(
        command,
        cwd=str(workspace),
        env=_hook_env(workspace, issue),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(),
            timeout=timeout_ms / 1000,
        )
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        logger.warning(
            "workspace.hook.timed_out",
            hook=hook_name,
            workspace=str(workspace),
            issue_identifier=issue.identifier,
            timeout_ms=timeout_ms,
        )
        raise TimeoutError(f"{hook_name} hook timed out after {timeout_ms}ms") from exc

    stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
    stderr = stderr_bytes.decode("utf-8", errors="replace").strip()
    logger.info(
        "workspace.hook.finished",
        hook=hook_name,
        workspace=str(workspace),
        issue_identifier=issue.identifier,
        returncode=process.returncode,
        stdout=stdout,
        stderr=stderr,
    )
    if process.returncode != 0:
        raise RuntimeError(f"{hook_name} hook failed with exit code {process.returncode}")


def _hook_env(workspace: Path, issue: Issue) -> dict[str, str]:
    env = dict(os.environ)
    env["SYMPHONY_WORKSPACE_PATH"] = str(workspace)
    for name, value in {
        "SYMPHONY_ISSUE_ID": getattr(issue, "id", None),
        "SYMPHONY_ISSUE_IDENTIFIER": getattr(issue, "identifier", None),
        "SYMPHONY_ISSUE_STATE": getattr(issue, "state", None),
        "SYMPHONY_ISSUE_TITLE": getattr(issue, "title", None),
        "SYMPHONY_ISSUE_URL": getattr(issue, "url", None),
    }.items():
        if value is not None:
            env[name] = str(value)
    return env
