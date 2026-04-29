from __future__ import annotations

import time
from pathlib import Path

from symphony.workflow import load, stamp

WORKFLOW_PATH = Path(__file__).resolve().parents[2] / "WORKFLOW.md"


def test_load_parses_real_workflow_fixture() -> None:
    workflow = load(WORKFLOW_PATH)

    assert {"tracker", "polling", "agent", "codex"} <= set(workflow.config)
    assert "{{ issue.identifier }}" in workflow.prompt_template


def test_stamp_is_stable_until_file_changes(tmp_path: Path) -> None:
    first = stamp(WORKFLOW_PATH)
    second = stamp(WORKFLOW_PATH)

    assert first == second

    fixture_copy = tmp_path / "WORKFLOW.md"
    fixture_copy.write_text(WORKFLOW_PATH.read_text())

    original_copy_stamp = stamp(fixture_copy)
    time.sleep(0.02)
    fixture_copy.write_text(f"{fixture_copy.read_text()}\n<!-- modified -->\n")

    assert stamp(fixture_copy) != original_copy_stamp
