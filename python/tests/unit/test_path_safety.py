from __future__ import annotations

from pathlib import Path

from symphony.path_safety import canonicalize, is_within, safe_identifier


def test_canonicalize_resolves_relative_paths(monkeypatch, tmp_path: Path) -> None:
    target = tmp_path / "root" / "child"
    target.mkdir(parents=True)

    monkeypatch.chdir(tmp_path)

    assert canonicalize(Path("root/child/../child")) == target.resolve(strict=False)


def test_canonicalize_resolves_symlinks(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    link = tmp_path / "link"
    link.symlink_to(actual, target_is_directory=True)

    assert canonicalize(link / "nested" / ".." / "file.txt") == (actual / "file.txt")


def test_is_within_checks_inside_outside_and_symlink_escapes(tmp_path: Path) -> None:
    root = tmp_path / "root"
    inside = root / "team"
    outside = tmp_path / "outside"
    root.mkdir()
    inside.mkdir()
    outside.mkdir()

    escape = root / "escape"
    escape.symlink_to(outside, target_is_directory=True)

    assert is_within(root, root)
    assert is_within(root, inside)
    assert is_within(root, inside / ".." / "team")
    assert not is_within(root, outside)
    assert not is_within(root, escape / "note.txt")


def test_safe_identifier_sanitizes_unsafe_characters() -> None:
    assert safe_identifier("ENG-123") == "ENG-123"
    assert safe_identifier("ENG/123 fix.me") == "ENG_123_fix_me"
    assert safe_identifier("already_ok__id") == "already_ok__id"
