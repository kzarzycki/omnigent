"""The pytest targets a change touches, for `mise run check-fast`: one per line, from the git diff.

Usage: python3 affected_tests.py [<base>]   (default: GATE_BASE, else merge base with origin/mine)
       python3 affected_tests.py --since <record> [<base>]

- `--since` builds on an earlier run's record (run-affected-tests.sh writes it: the tested tree's
  `git ls-tree -r -z`, the selected targets, pytest's lastfailed and exit code). Of today's
  selection it prints the tests that failed then, the targets of the files changed between the two
  trees, and the targets the earlier run didn't select; stderr says why each runs, and names as a
  flake candidate each failed test nothing changed reselects. A record whose pytest didn't run to
  its end (exit other than 0 or 1), or one it can't read, prints today's selection.
- A changed `test_*.py` runs. Another changed Python file runs every test file that names its
  module (`omnigent/a/b.py`: `omnigent.a.b`, or `from omnigent.a import b`; `__init__.py`: its
  package) or its file name; any other non-Markdown file, the test files that name
  its file name.
- A conftest below `tests/` selects its own directory, since pytest applies it only there; one in
  a suite check-fast doesn't run selects nothing.
- Markdown selects nothing. No base to diff, or a change to the shared test setup (pyproject.toml,
  uv.lock, `tests/conftest.py`, the shared test helpers and fixtures), selects `tests`: the
  diff cannot say what that touches.

ponytail: a name match, adapted from agent-skills' tools/affected_tests.py without its coverage
test map (the map needs a nightly to build and restore it). Upgrade path: add the map once a
nightly exists.
"""

from __future__ import annotations

import argparse
import json
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

    def shared(f: str) -> bool:
        path = Path(f)
        return f.startswith(EVERYTHING) or (
            path.name == "conftest.py" and (path.parts[0] != "tests" or len(path.parts) < 3)
        )

    if files is None or any(shared(f) for f in files):
        return ["tests"]
    tests = {
        p: p.read_text(errors="ignore")
        for p in sorted((root / "tests").rglob("test_*.py"))
        if p.relative_to(root).parts[1] not in NOT_RUN
    }
    picked: set[str] = set()
    for f in files:
        path = Path(f)
        if path.name == "conftest.py":
            if path.parts[1] not in NOT_RUN and (root / path).parent.is_dir():
                picked.add(str(path.parent))
        elif path.name.startswith("test_") and path.suffix == ".py":
            if (root / path).exists() and path.parts[1:2] and path.parts[1] not in NOT_RUN:
                picked.add(f)
        elif path.suffix != ".md":
            picked.update(
                str(t.relative_to(root)) for t, text in tests.items() if names(path, text)
            )
    # a file inside a selected directory already runs with it
    return sorted(t for t in picked if not any(o != t and under(t, [o]) for o in picked))


def listing(text: str) -> dict[str, str]:
    """`git ls-tree -r -z` output as path -> mode, type and blob id."""
    entries = (entry.split("\t", 1) for entry in text.split("\0") if entry)
    return {path: meta for meta, path in entries}


def under(target: str, paths: list[str]) -> bool:
    """Whether a target (a path or a node id) is one of `paths` or inside one of them."""
    path = target.split("::", 1)[0]
    return any(target == p or path == p or path.startswith(p.rstrip("/") + "/") for p in paths)


def within(found: list[str], selection: list[str]) -> list[str]:
    """The part of `found` inside `selection`: a directory in `found` stands for the selected files
    under it; a target outside the selection runs nowhere today, so it doesn't run here."""
    kept = {t for t in found if under(t, selection)}
    kept.update(s for s in selection for t in found if "::" not in t and under(s, [t]))
    return sorted(kept)


def since(
    record: Path, selection: list[str], root: Path = Path(".")
) -> tuple[list[str], list[str]] | str:
    """What to run on HEAD after the run `record` describes, and its flake candidates; or why the
    record can't be used."""
    try:
        exit_code = (record / "exit").read_text().strip()
        then = listing((record / "tree").read_text())
        selected_then = [t for t in (record / "selected").read_text().split("\n") if t]
        lastfailed = json.loads((record / "lastfailed.json").read_text())
    except (OSError, UnicodeDecodeError, ValueError) as error:
        return f"the record can't be read: {error}"
    if exit_code not in ("0", "1"):
        return f"pytest exited {exit_code or 'without a code'} in that run"
    if not then or not isinstance(lastfailed, dict):
        return "the record holds no tree listing or no lastfailed mapping"
    # pytest also exits 1 when it dies before its session starts, and then names no failed test
    if exit_code == "1" and not lastfailed:
        return "pytest exited 1 with no failed test recorded, so it may not have run"
    now = git("ls-tree", "-r", "-z", "HEAD")
    if now is None:
        return "git ls-tree HEAD failed"
    now_tree = listing(now)
    differ = sorted(p for p in then.keys() | now_tree.keys() if then.get(p) != now_tree.get(p))
    touched = within(targets(differ, root), selection)
    new = [t for t in selection if not under(t, selected_then)]
    failed = within([t for t in lastfailed if (root / t.split("::", 1)[0]).exists()], selection)
    picked = set(touched) | set(new) | set(failed)
    # a node id or file inside a picked file or directory already runs with it
    picked = {
        t for t in picked if not any(o != t and "::" not in o and under(t, [o]) for o in picked)
    }
    candidates = [t for t in failed if t in picked and not under(t, touched + new)]
    shown = " ".join(differ[:20]) + (f" and {len(differ) - 20} more" if len(differ) > 20 else "")
    print(
        f"affected tests: building on {record}: {len(lastfailed)} failed there;"
        f" {len(differ)} files differ since: {shown or 'none'}",
        file=sys.stderr,
    )
    for t in sorted(picked):
        why = [
            reason
            for reason, part in (
                ("failed in that run", failed),
                ("names a file that differs", touched),
                ("selected now, not in that run", new),
            )
            if under(t, part)
        ]
        print(f"affected tests: {t}: {', '.join(why)}", file=sys.stderr)
    for t in candidates:
        print(f"affected tests: flake candidate: {t}", file=sys.stderr)
    return sorted(picked), candidates


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="The pytest targets a change touches.")
    parser.add_argument("base", nargs="?")
    parser.add_argument("--since", type=Path, help="an earlier run's record to build on")
    args = parser.parse_args()
    base = args.base if args.base is not None else os.environ.get("GATE_BASE")
    base = base or (git("merge-base", "origin/mine", "HEAD") or "").strip() or None
    selected = targets(changed(base))
    if args.since is not None:
        rerun = since(args.since, selected)
        if isinstance(rerun, str):
            print(
                f"affected tests: no usable record ({rerun}); running the full selection",
                file=sys.stderr,
            )
        else:
            selected = rerun[0]
    print("\n".join(selected))
