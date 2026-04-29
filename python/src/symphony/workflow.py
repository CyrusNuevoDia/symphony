from __future__ import annotations

from hashlib import blake2b
from pathlib import Path

import frontmatter
from pydantic import BaseModel, Field


class Workflow(BaseModel):
    config: dict[str, object] = Field(default_factory=dict)
    prompt_template: str = ""


def parse(text: str) -> Workflow:
    front_matter_lines, prompt_lines = _split_front_matter(text)
    yaml = "\n".join(front_matter_lines).strip()
    prompt_template = "\n".join(prompt_lines).strip()
    return Workflow(config=_parse_front_matter(yaml), prompt_template=prompt_template)


def load(path: Path) -> Workflow:
    return parse(path.read_text())


def stamp(path: Path) -> tuple[float, int, str]:
    data = path.read_bytes()
    stat = path.stat()
    return stat.st_mtime, stat.st_size, blake2b(data).hexdigest()


def _split_front_matter(text: str) -> tuple[list[str], list[str]]:
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        return [], lines

    front_matter_lines: list[str] = []
    for index, line in enumerate(lines[1:], start=1):
        if line == "---":
            return front_matter_lines, lines[index + 1 :]
        front_matter_lines.append(line)

    return front_matter_lines, []


def _parse_front_matter(yaml: str) -> dict[str, object]:
    if not yaml:
        return {}

    config, _ = frontmatter.parse(f"---\n{yaml}\n---\n")
    if not isinstance(config, dict):
        raise TypeError("workflow frontmatter must decode to a mapping")
    return dict(config)
