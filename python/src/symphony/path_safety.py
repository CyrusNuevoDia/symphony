from __future__ import annotations

import re
from pathlib import Path

_UNSAFE_IDENTIFIER = re.compile(r"[^A-Za-z0-9_-]+")


def canonicalize(path: Path | str) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def is_within(root: Path | str, candidate: Path | str) -> bool:
    canonical_root = canonicalize(root)
    canonical_candidate = canonicalize(candidate)
    return canonical_candidate == canonical_root or canonical_root in canonical_candidate.parents


def safe_identifier(identifier: str) -> str:
    return _UNSAFE_IDENTIFIER.sub("_", identifier or "issue")
