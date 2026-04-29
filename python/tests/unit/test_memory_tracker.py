from __future__ import annotations

import pytest

from symphony.tracker import Issue
from symphony.tracker.memory import MemoryTracker


def _issue(issue_id: str, *, state: str, title: str) -> Issue:
    return Issue(
        id=issue_id,
        identifier=f"ENG-{issue_id}",
        title=title,
        state=state,
        url=f"https://example.com/{issue_id}",
    )


@pytest.mark.anyio
async def test_memory_tracker_fetch_methods_and_state_seams() -> None:
    tracker = MemoryTracker()
    tracker.add_issue(_issue("1", state="Todo", title="First"))
    tracker.add_issue(_issue("2", state="In Progress", title="Second"))
    tracker.add_issue(_issue("3", state="Done", title="Third"))

    candidates = await tracker.fetch_candidate_issues()
    active = await tracker.fetch_issues_by_states(["todo", "in progress"])
    by_id = await tracker.fetch_issue_states_by_ids(["2", "3"])

    assert [issue.id for issue in candidates] == ["1", "2", "3"]
    assert [issue.id for issue in active] == ["1", "2"]
    assert [issue.id for issue in by_id] == ["2", "3"]

    tracker.set_state("1", "Human Review")
    refreshed = await tracker.fetch_issue_states_by_ids(["1"])
    assert refreshed[0].state == "Human Review"


@pytest.mark.anyio
async def test_memory_tracker_records_comments_and_state_updates() -> None:
    tracker = MemoryTracker()
    tracker.add_issue(_issue("7", state="Todo", title="Tracked"))

    await tracker.create_comment("7", "Workpad updated")
    await tracker.update_issue_state("7", "Done")

    refreshed = await tracker.fetch_issue_states_by_ids(["7"])

    assert tracker.comments == [("7", "Workpad updated")]
    assert tracker.state_updates == [("7", "Done")]
    assert refreshed[0].state == "Done"
