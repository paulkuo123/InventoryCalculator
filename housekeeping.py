"""Small, best-effort cleanup helpers for generated project artifacts."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Iterable, Sequence


def remove_files(paths: Iterable[str | Path | None]) -> list[Path]:
    """Remove explicit file paths and return the paths that could not be removed."""
    failed: list[Path] = []
    for raw_path in paths:
        if not raw_path:
            continue
        path = Path(raw_path)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            failed.append(path)
    return failed


def prune_generated_files(
    directory: str | Path,
    patterns: Sequence[str],
    *,
    keep: int,
) -> list[Path]:
    """Keep the newest generated files matching the given glob patterns."""
    root = Path(directory)
    if keep < 0 or not root.is_dir():
        return []

    candidates: dict[Path, int] = {}
    for pattern in patterns:
        for path in root.glob(pattern):
            if not path.is_file():
                continue
            try:
                candidates[path] = path.stat().st_mtime_ns
            except OSError:
                continue

    ordered = sorted(candidates, key=lambda path: (candidates[path], path.name), reverse=True)
    stale = ordered[keep:]
    failed = set(remove_files(stale))
    return [path for path in stale if path not in failed]


def remove_stale_matching_files(
    directory: str | Path,
    patterns: Sequence[str],
    *,
    older_than_seconds: int,
    now: float | None = None,
) -> list[Path]:
    """Remove old files only when their names match an explicit project pattern."""
    root = Path(directory)
    if older_than_seconds < 0 or not root.is_dir():
        return []

    cutoff = (time.time() if now is None else now) - older_than_seconds
    stale: list[Path] = []
    for pattern in patterns:
        for path in root.glob(pattern):
            if not path.is_file():
                continue
            try:
                if path.stat().st_mtime < cutoff:
                    stale.append(path)
            except OSError:
                continue

    unique = list(dict.fromkeys(stale))
    failed = set(remove_files(unique))
    return [path for path in unique if path not in failed]
