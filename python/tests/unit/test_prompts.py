from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from symphony.workflow import load

WORKFLOW_PATH = Path(__file__).resolve().parents[2] / "WORKFLOW.md"


def _issue_model():
    try:
        from symphony.tracker import Issue
    except ModuleNotFoundError:
        pytest.xfail("symphony.tracker is not available yet from WS-1A")
    return Issue


def test_build_turn_prompt_renders_attempt_context() -> None:
    Issue = _issue_model()
    from symphony.prompts import build_turn_prompt

    workflow = load(WORKFLOW_PATH)
    now = datetime.now(UTC)
    issue = Issue(
        id="issue-1",
        identifier="ENG-123",
        title="Fix prompt rendering",
        description="Make sure the Liquid workflow renders correctly.",
        state="In Progress",
        priority=2,
        branch_name="eng-123-fix-prompts",
        url="https://linear.example/ENG-123",
        assignee_id="user-1",
        blocked_by=[],
        labels=["python", "workflow"],
        assigned_to_worker=True,
        created_at=now,
        updated_at=now,
    )

    without_retry = build_turn_prompt(issue, workflow, attempt=None)
    with_retry = build_turn_prompt(issue, workflow, attempt=2)

    assert issue.identifier in without_retry
    assert issue.title in without_retry
    assert "retry attempt #2" not in without_retry
    assert issue.identifier in with_retry
    assert issue.title in with_retry
    assert "retry attempt #2" in with_retry
