"""The pytest targets a change touches, for `mise run check-fast`: one per line, from the git diff.

Usage: python3 affected_tests.py [<base>]   (default: GATE_BASE, else merge base with origin/mine)

- A changed `test_*.py` runs. Another changed Python file runs every test file that names its
  module (`omnigent/a/b.py`: `omnigent.a.b`, or `from omnigent.a import b`; `__init__.py`: its
  package) or its file name; any other non-Markdown file, the test files that name
  its file name.
- Markdown selects nothing. No base to diff, or a change to the test setup (pyproject.toml,
  uv.lock, a conftest, the shared test helpers and fixtures), selects `tests`: the
  diff cannot say what that touches.

ponytail: a name match, adapted from agent-skills' tools/affected_tests.py without its coverage
test map (the map needs a nightly to build and restore it). Upgrade path: add the map once a
nightly exists.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

# the suites pyproject.toml's addopts ignore: they need a server or a browser
NOT_RUN = {"e2e", "e2e_ui", "e2e_live", "browser_ui", "integration"}
EVERYTHING = (
    "pyproject.toml",
    "uv.lock",
    "tests/_helpers/",
    "tests/helpers/",
    "tests/_fixtures/",
    "tests/fixtures/",
    "tests/data/",
)


def git(*args: str) -> str | None:
    done = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    return done.stdout if done.returncode == 0 else None


def changed(base: str | None) -> list[str] | None:
    out = git("diff", "--name-only", "--no-renames", f"{base}...HEAD") if base else None
    return out.split("\n") if out is not None else None


def names(path: Path, text: str) -> bool:
    """Whether a test's `text` names the file: its file name, or for Python its dotted module."""
    if path.name in text:
        return True
    if path.suffix != ".py":
        return False
    parts = list(path.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    dotted, parent = ".".join(parts), ".".join(parts[:-1])
    if dotted in text:
        return True
    # `from pkg import module` and `from pkg import (a, module)`
    stem = re.escape(parts[-1])
    return bool(parent) and bool(
        re.search(rf"from {re.escape(parent)} import[^\n]*\b{stem}\b", text)
        or re.search(rf"from {re.escape(parent)} import \([^)]*\b{stem}\b", text)
    )


def targets(files: list[str] | None, root: Path = Path(".")) -> list[str]:
    files = [f for f in files if f] if files is not None else None
    if files is None or any(
        f.startswith(EVERYTHING) or Path(f).name == "conftest.py" for f in files
    ):
        return ["tests"]
    tests = {
        p: p.read_text(errors="ignore")
        for p in sorted((root / "tests").rglob("test_*.py"))
        if p.relative_to(root).parts[1] not in NOT_RUN
    }
    picked: set[str] = set()
    for f in files:
        path = Path(f)
        if path.name.startswith("test_") and path.suffix == ".py":
            if (root / path).exists() and path.parts[1:2] and path.parts[1] not in NOT_RUN:
                picked.add(f)
        elif path.suffix != ".md":
            picked.update(
                str(t.relative_to(root)) for t, text in tests.items() if names(path, text)
            )
    return sorted(picked)


if __name__ == "__main__":
    base = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("GATE_BASE")
    base = base or (git("merge-base", "origin/mine", "HEAD") or "").strip() or None
    print("\n".join(targets(changed(base))))
