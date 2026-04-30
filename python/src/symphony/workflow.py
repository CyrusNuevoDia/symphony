from __future__ import annotations

from hashlib import blake2b
from pathlib import Path

import frontmatter
from pydantic import BaseModel, Field


class Workflow(BaseModel):
    config: dict[str, object] = Field(default_factory=dict)
    prompt_template: str = ""


def parse(text: str) -> Workflow:
    post = frontmatter.loads(text)
    if not isinstance(post.metadata, dict):
        raise TypeError("workflow frontmatter must decode to a mapping")
    return Workflow(config=dict(post.metadata), prompt_template=post.content.strip())


def load(path: Path) -> Workflow:
    return parse(path.read_text())


def stamp(path: Path) -> tuple[float, int, str]:
    data = path.read_bytes()
    stat = path.stat()
    return stat.st_mtime, stat.st_size, blake2b(data).hexdigest()
