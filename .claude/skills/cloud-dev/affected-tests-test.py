"""affected_tests.py on a temporary git repo: what each kind of change selects.

Usage: python3 affected-tests-test.py
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import affected_tests  # noqa: E402


def run(*cmd: str) -> None:
    subprocess.run(cmd, check=True, capture_output=True)


def select(
    repo: Path, files: dict[str, str], args: tuple[str, ...] = ("base",), **env: str
) -> list[str]:
    """Commit `files` (path -> text) on the base commit; return what the selector prints."""
    run("git", "checkout", "-q", "-B", "work", "base")
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    run("git", "add", "-A")
    run("git", "commit", "-q", "-m", "change")
    out = subprocess.run(
        [sys.executable, str(HERE / "affected_tests.py"), *args],
        env={**{k: v for k, v in os.environ.items() if k != "GATE_BASE"}, **env},
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.split()


with tempfile.TemporaryDirectory() as tmp:
    repo = Path(tmp)
    os.chdir(repo)
    run("git", "init", "-q", "-b", "main")
    run("git", "config", "user.email", "t@example.com")
    run("git", "config", "user.name", "t")
    (repo / "tests" / "server").mkdir(parents=True)
    (repo / "omnigent" / "widgets").mkdir(parents=True)
    (repo / "tests" / "server" / "test_widget.py").write_text(
        "from omnigent.widgets import frob\n"
    )
    (repo / "tests" / "server" / "test_other.py").write_text(
        "import omnigent.gadget  # see README.md\n"
    )
    (repo / "omnigent" / "widgets" / "frob.py").write_text("x = 1\n")
    (repo / "omnigent" / "widgets" / "__init__.py").write_text("")
    (repo / "README.md").write_text("hi\n")
    run("git", "add", "-A")
    run("git", "commit", "-q", "-m", "base")
    run("git", "tag", "base")

    widget = "tests/server/test_widget.py"
    assert select(repo, {"tests/server/test_widget.py": "# edited\n"}) == [widget]
    assert select(repo, {"omnigent/widgets/frob.py": "x = 2\n"}) == [widget]
    assert select(repo, {"omnigent/widgets/__init__.py": "# p\n"}) == [widget]
    assert select(repo, {"README.md": "more\n"}) == []
    assert select(repo, {"omnigent/unused.py": "y = 1\n"}) == []
    assert select(repo, {"tests/server/conftest.py": "# c\n"}) == ["tests"]
    assert select(repo, {"pyproject.toml": "[x]\n"}) == ["tests"]
    assert select(repo, {"tests/helpers/util.py": "z = 1\n"}) == ["tests"]
    # suites the default addopts ignore are never selected
    (repo / "tests" / "e2e").mkdir()
    (repo / "tests" / "e2e" / "test_live.py").write_text("import omnigent.widgets.frob\n")
    run("git", "add", "-A")
    run("git", "commit", "-q", "-m", "e2e")
    run("git", "tag", "-f", "base")
    assert select(repo, {"omnigent/widgets/frob.py": "x = 3\n"}) == [widget]
    # no argument: GATE_BASE, else the merge base with origin/mine, else everything
    change = {"omnigent/widgets/frob.py": "x = 4\n"}
    assert select(repo, change, (), GATE_BASE="base") == [widget]
    assert select(repo, change, (), GATE_BASE="nonexistent") == ["tests"]
    run("git", "update-ref", "refs/remotes/origin/mine", "base")
    assert select(repo, change, ()) == [widget]
    run("git", "update-ref", "-d", "refs/remotes/origin/mine")
    assert select(repo, change, ()) == ["tests"]
    assert affected_tests.targets(None) == ["tests"]  # no base to diff
    # narrow (a PR head): only the changed test files, even for a module or conftest change
    assert select(repo, {"omnigent/widgets/frob.py": "x = 5\n"}, (), GATE_BASE="base", AFFECTED_TESTS_NARROW="1") == []
    assert select(repo, {"tests/server/conftest.py": "# c\n"}, (), GATE_BASE="base", AFFECTED_TESTS_NARROW="1") == []
    assert select(repo, {widget: "# e\n", "omnigent/widgets/frob.py": "x = 6\n"}, (), GATE_BASE="base", AFFECTED_TESTS_NARROW="1") == [widget]
    # a deleted test file selects nothing, not a path pytest can't find
    run("git", "checkout", "-q", "-B", "work", "base")
    (repo / widget).unlink()
    run("git", "commit", "-qam", "drop")
    assert affected_tests.changed("base") == [widget, ""]
    assert affected_tests.targets(affected_tests.changed("base")) == []
print("affected-tests: ok")
