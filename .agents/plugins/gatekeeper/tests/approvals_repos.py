"""The git repos the approvals tests share: `git_in`, and `committed`, a copy of a template repo built once per session.

A leaf module, since approvals_harness.py imports test_approvals.py and test_approvals.py needs these: it re-exports
them, so a module takes them from the harness as before. The template is `repo_template` (an AUTOUSE fixture the
harness defines, so only `test_approvals*` modules build it).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

TEMPLATE: dict[str, Path] = {}  # the session's one built repo, set by the harness's `repo_template`


def git_in(where: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "protocol.file.allow=always", "-C", str(where), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def build_template(where: Path) -> Path:
    """The repo every `committed` copy starts from: one file, one commit. Built once per session (per xdist worker)."""
    where.mkdir(exist_ok=True)
    git_in(where, "init", "-q")
    git_in(where, "config", "user.email", "loop@example.com")
    git_in(where, "config", "user.name", "loop")
    (where / "app.py").write_text("value = 1\n")
    git_in(where, "add", "-A")
    git_in(where, "commit", "-qm", "base")
    TEMPLATE["repo"] = where
    return where


def committed(where: Path) -> str:
    """`where` as a copy of the session's template repo: independent of it and of every other test's copy."""
    shutil.copytree(TEMPLATE["repo"], where, symlinks=True, dirs_exist_ok=True)
    return git_in(where, "rev-parse", "HEAD").strip()
