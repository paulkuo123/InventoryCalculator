"""Safely update the project-local environment file."""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path


def read_existing_lines(path: str | os.PathLike[str]) -> list[str]:
    destination = Path(path)
    if not destination.exists():
        return []
    return destination.read_text(encoding="utf-8").splitlines()


def upsert_env_value(
    lines: list[str],
    name: str,
    value: str,
    *,
    overwrite: bool = True,
) -> list[str]:
    result: list[str] = []
    found = False
    for line in lines:
        if re.match(rf"(?:export\s+)?{re.escape(name)}=", line.strip()):
            result.append(f'{name}="{value}"' if overwrite else line)
            found = True
        else:
            result.append(line)
    if not found:
        if result and result[-1].strip():
            result.append("")
        result.append(f'{name}="{value}"')
    return result


def atomic_write_local_env(
    path: str | os.PathLike[str],
    lines: list[str],
) -> None:
    """Atomically replace an env file and keep its secrets owner-only."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(destination.parent),
            prefix=f".{destination.name.lstrip('.') or 'env'}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            os.chmod(handle.name, 0o600)
            handle.write("\n".join(lines).rstrip() + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, destination)
        temporary_path = None
        os.chmod(destination, 0o600)
    finally:
        if temporary_path and temporary_path.exists():
            temporary_path.unlink()
