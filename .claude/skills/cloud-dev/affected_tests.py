"""The pytest targets a change touches, for `mise run check-fast`: one per line, from the git diff.

Usage: python3 affected_tests.py [<base>]   (default: GATE_BASE, else merge base with origin/mine)
       python3 affected_tests.py --since <record> [<base>]

- `--since` builds on an earlier run's record (run-affected-tests.sh writes it: the tested tree's
  `git ls-tree -r -z`, the selected targets, pytest's lastfailed and exit code). Of today's
  selection it prints the tests that failed then, the targets of the files changed between the two
  trees, and the targets the earlier run didn't select; stderr says why each runs. A failed test
  nothing changed reselects is a flake candidate, and then today's whole selection prints: alone, a
  test that fails only after other tests (left-over state) passes and would look like a flake. A
  record whose pytest didn't run to its end (exit other than 0 or 1), or one it can't read, prints
  today's selection.
- A changed `test_*.py` runs. Another changed Python file runs every test file that names its
  module (`omnigent/a/b.py`: `omnigent.a.b`, or `from omnigent.a import b`; `__init__.py`: its
  package) or its file name; any other non-Markdown file, the test files that name
  its file name.
- A change under `omnigent/` also runs every test file that starts Omnigent as a subprocess
  (`-m omnigent`, `"-m", "omnigent"`, or the `omnigent` entry point): it imports the whole package.
- A conftest below `tests/` selects its own directory, since pytest applies it only there (none
  in a suite check-fast doesn't run), and the test files that import it by module or load it
  through `pytest_plugins`.
- With AFFECTED_TESTS_BUILD_INPUTS=floor (the merge queue's gate; the nightly covers the rest after
  the merge), pyproject.toml and uv.lock no longer select `tests`: like any other file they select
  the tests that name them, a uv.lock change adds the tests that import a package whose version
  moved, and the other changed files select as usual.
- Markdown selects nothing. No base to diff, or a change to the shared test setup (pyproject.toml,
  uv.lock, `tests/conftest.py`, the shared test helpers and fixtures), selects `tests`: the
  diff cannot say what that touches.

ponytail: a name match, adapted from agent-skills' tools/affected_tests.py without its coverage
test map (the map needs a nightly to build and restore it). Upgrade path: add the map once a
nightly exists.
"""

from __future__ import annotations

import argparse
import functools
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


def names(path: Path, text: str, by_file_name: bool = True) -> bool:
    """Whether a test's `text` names the file: its file name, or for Python its dotted module."""
    if by_file_name and path.name in text:
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


# a test that starts Omnigent as a subprocess: `-m omnigent`, `"-m", "omnigent"`, or the entry point
SUBPROCESS_START = re.compile(
    r"""["']-m["']\s*,\s*["']omnigent["']|-m omnigent\b|\[\s*["']omnigent["']\s*,"""
)


# the build inputs the queue's floor leaves to the nightly
FLOOR_INPUTS = ("pyproject.toml", "uv.lock")


# distributions whose import name differs from the distribution name, beyond what the installed
# metadata says (the selector can run on an interpreter without the project's dependencies)
IMPORT_NAMES = {
    "pyyaml": "yaml",
    "pillow": "PIL",
    "python-dateutil": "dateutil",
    "protobuf": "google",
}


def locked_versions(text: str | None) -> dict[str, str]:
    """Package name -> version from a uv.lock's text; empty for none. The project's own packages
    (an editable or virtual source) are left out: every test imports them."""
    found: dict[str, str] = {}
    for block in (text or "").split("[[package]]")[1:]:
        name = re.search(r'^name = "([^"]+)"', block, re.MULTILINE)
        version = re.search(r'^version = "([^"]+)"', block, re.MULTILINE)
        own = re.search(r"^source = \{ (?:editable|virtual) =", block, re.MULTILINE)
        if name and version and not own:
            found[re.sub(r"[-_.]+", "-", name.group(1)).lower()] = version.group(1)
    return found


def moved_between(before: str | None, after: str | None) -> set[str]:
    """Packages whose locked version differs between two uv.lock texts (added or removed count)."""
    old, new = locked_versions(before), locked_versions(after)
    return {name for name in old.keys() | new.keys() if old.get(name) != new.get(name)}


def moved_packages(base: str | None) -> set[str]:
    """Packages whose locked version differs between `base` and HEAD."""
    before = git("show", f"{base}:uv.lock") if base else None
    return moved_between(before, git("show", "HEAD:uv.lock"))


@functools.lru_cache(maxsize=1)
def module_owners() -> dict[str, tuple[str, ...]]:
    """Installed top-level module -> normalised distribution names providing it, read once."""
    from importlib.metadata import packages_distributions

    return {
        module: tuple(re.sub(r"[-_.]+", "-", name).lower() for name in names)
        for module, names in packages_distributions().items()
    }


def import_names(package: str) -> set[str]:
    """The module names a distribution provides: those the installed environment records for it
    (RECORD, so `opentelemetry-api` gives `opentelemetry`), a known alias, and its own name with
    `-` as `_` (all there is for a distribution not installed here, which no test can import)."""
    names = {package.replace("-", "_")}
    if package in IMPORT_NAMES:
        names.add(IMPORT_NAMES[package])
    names.update(module for module, owners in module_owners().items() if package in owners)
    return names


def imports_any(moved: set[str]) -> re.Pattern[str] | None:
    """One pattern matching an import of a module a moved package provides, else None."""
    modules = sorted({module for package in moved for module in import_names(package)})
    if not modules:
        return None
    alternation = "|".join(re.escape(module) for module in modules)
    # an import line naming one (also after a comma), or a string naming one (patch, importorskip)
    return re.compile(
        rf"^\s*(?:import|from)\b[^\n#]*\b(?:{alternation})\b"
        rf"|[\"'](?:{alternation})(?:\.|[\"'])",
        re.MULTILINE,
    )


def targets(
    files: list[str] | None,
    root: Path = Path("."),
    floor: bool = False,
    moved: set[str] | None = None,
) -> list[str]:
    """The test targets `files` touch; with `floor`, pyproject.toml and uv.lock select only through
    the other files and the tests importing a package in `moved`."""
    files = [f for f in files if f] if files is not None else None

    def shared(f: str) -> bool:
        path = Path(f)
        if floor and f in FLOOR_INPUTS:
            return False
        # a deployed pack's conftest applies only to the pack's tests, which run in its own repo
        return f.startswith(EVERYTHING) or (
            path.name == "conftest.py"
            and path.parts[0] != ".agents"
            and (path.parts[0] != "tests" or len(path.parts) < 3)
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
        if path.name.startswith("test_") and path.suffix == ".py":
            # .agents/ is the deployed pack; its tests run in the pack's own repo.
            if (
                (root / path).exists()
                and path.parts[1:2]
                and path.parts[0] != ".agents"
                and path.parts[1] not in NOT_RUN
            ):
                picked.add(f)
        elif path.suffix != ".md":
            if f == "uv.lock" and floor and (pattern := imports_any(moved or set())):
                picked.update(
                    str(t.relative_to(root)) for t, text in tests.items() if pattern.search(text)
                )
            if path.parts[0] == "omnigent":
                # a subprocess test imports the whole package, whatever its text names
                picked.update(
                    str(t.relative_to(root))
                    for t, text in tests.items()
                    if SUBPROCESS_START.search(text)
                )
            conftest = path.name == "conftest.py"
            if (
                conftest
                and path.parts[0] != ".agents"
                and path.parts[1] not in NOT_RUN
                and (root / path).parent.is_dir()
            ):
                picked.add(str(path.parent))
            # a conftest by module only: many tests mention the file name `conftest.py`
            picked.update(
                str(t.relative_to(root))
                for t, text in tests.items()
                if names(path, text, by_file_name=not conftest)
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
    record: Path, selection: list[str], root: Path = Path("."), floor: bool = False
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
    moved = None
    if floor and "uv.lock" in differ and "uv.lock" in then:
        blob = then["uv.lock"].split()[-1]
        moved = moved_between(git("cat-file", "-p", blob), git("show", "HEAD:uv.lock"))
    touched = within(targets(differ, root, floor=floor, moved=moved), selection)
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
    if candidates:
        for t in candidates:
            print(f"affected tests: flake candidate: {t}", file=sys.stderr)
        print(
            f"affected tests: nothing changed reselects {len(candidates)} failed test(s), so the"
            " whole selection runs: alone, a test that fails only after other tests would pass",
            file=sys.stderr,
        )
        return sorted(selection), candidates
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
    return sorted(picked), []


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="The pytest targets a change touches.")
    parser.add_argument("base", nargs="?")
    parser.add_argument("--since", type=Path, help="an earlier run's record to build on")
    args = parser.parse_args()
    base = args.base if args.base is not None else os.environ.get("GATE_BASE")
    base = base or (git("merge-base", "origin/mine", "HEAD") or "").strip() or None
    floor = os.environ.get("AFFECTED_TESTS_BUILD_INPUTS") == "floor"
    selected = targets(changed(base), floor=floor, moved=moved_packages(base) if floor else None)
    if args.since is not None:
        rerun = since(args.since, selected, floor=floor)
        if isinstance(rerun, str):
            print(
                f"affected tests: no usable record ({rerun}); running the full selection",
                file=sys.stderr,
            )
        else:
            selected = rerun[0]
    print("\n".join(selected))
