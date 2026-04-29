from __future__ import annotations

from typing import TYPE_CHECKING

from liquid import Environment
from liquid.undefined import StrictUndefined

from symphony.workflow import Workflow

if TYPE_CHECKING:
    from symphony.tracker import Issue

_ENV = Environment(undefined=StrictUndefined, strict_filters=True)


def build_turn_prompt(issue: Issue, workflow: Workflow, attempt: int | None = None) -> str:
    template = _ENV.from_string(workflow.prompt_template)
    return template.render(
        attempt=attempt,
        issue=issue.model_dump(mode="json"),
    )
