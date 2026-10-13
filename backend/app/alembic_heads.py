"""Alembic head introspection for verify_runtime (H2).

The pinned head is derived from the migration scripts on disk — the pin is
the code, not configuration. Single head is enforced: multiple heads raise.
"""

from __future__ import annotations

import os
import re

_VERSIONS_DIR = os.path.join(os.path.dirname(__file__), "..", "alembic", "versions")


def _parse_revision(path: str) -> tuple[str | None, str | None]:
    text = open(path, encoding="utf-8").read()
    rev = re.search(r"^revision\s*=\s*['\"]([^'\"]+)['\"]", text, re.M)
    down = re.search(r"^down_revision\s*=\s*['\"]([^'\"]+)['\"]", text, re.M)
    return (rev.group(1) if rev else None, down.group(1) if down else None)


def head_revision(versions_dir: str | None = None) -> str:
    """Return the single head revision; raise if zero or multiple heads."""
    directory = os.path.abspath(versions_dir or _VERSIONS_DIR)
    revisions: dict[str, str | None] = {}
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".py") or name.startswith("_"):
            continue
        rev, down = _parse_revision(os.path.join(directory, name))
        if rev:
            revisions[rev] = down
    if not revisions:
        raise RuntimeError("no alembic revisions found")
    children = {d for d in revisions.values() if d}
    heads = [r for r in revisions if r not in children]
    if len(heads) != 1:
        raise RuntimeError(f"expected exactly one alembic head, found {heads}")
    return heads[0]


def linear_history(versions_dir: str | None = None) -> list[str]:
    """Revisions from base to head following down_revision links."""
    directory = os.path.abspath(versions_dir or _VERSIONS_DIR)
    revisions: dict[str, str | None] = {}
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".py") or name.startswith("_"):
            continue
        rev, down = _parse_revision(os.path.join(directory, name))
        if rev:
            revisions[rev] = down
    head = head_revision(versions_dir)
    history, seen, current = [], set(), head
    while current and current not in seen:
        seen.add(current)
        history.append(current)
        current = revisions.get(current)
    return list(reversed(history))
