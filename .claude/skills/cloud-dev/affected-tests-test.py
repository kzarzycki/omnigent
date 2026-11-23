"""affected_tests.py on a temporary git repo: what each kind of change selects.

Usage: python3 affected-tests-test.py
"""

import json
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
    repo = Path(tmp) / "repo"  # records stay outside it
    repo.mkdir()
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
    # an empty base argument skips GATE_BASE, as before
    assert select(repo, change, ("",), GATE_BASE="base") == ["tests"]
    assert affected_tests.targets(None) == ["tests"]  # no base to diff
    # a deleted test file selects nothing, not a path pytest can't find
    run("git", "checkout", "-q", "-B", "work", "base")
    (repo / widget).unlink()
    run("git", "commit", "-qam", "drop")
    assert affected_tests.changed("base") == [widget, ""]
    assert affected_tests.targets(affected_tests.changed("base")) == []

    # --since: a run on commit `then` left a record; what runs on HEAD now
    other, third = "tests/server/test_other.py", "tests/server/test_third.py"
    failed = f"{widget}::test_frob[a b]"  # a node id with a space stays whole
    run("git", "checkout", "-q", "-B", "work", "base")
    (repo / third).write_text("import omnigent.thing\n")
    (repo / "omnigent" / "thing.py").write_text("t = 1\n")
    run("git", "add", "-A")
    run("git", "commit", "-q", "-m", "third")
    run("git", "tag", "-f", "base")

    def commit(files: dict[str, str]) -> None:
        for name, text in files.items():
            (repo / name).write_text(text)
        run("git", "add", "-A")
        run("git", "commit", "-q", "--allow-empty", "-m", "change")

    def record(
        commit_id: str, selected: list[str], lastfailed: object, exit_code: str = "1"
    ) -> Path:
        path = Path(tempfile.mkdtemp(dir=tmp, prefix="record-"))
        tree = subprocess.run(["git", "ls-tree", "-r", "-z", commit_id], capture_output=True)
        (path / "tree").write_bytes(tree.stdout)
        (path / "selected").write_text("".join(f"{t}\n" for t in selected))
        (path / "lastfailed.json").write_text(json.dumps(lastfailed))
        (path / "exit").write_text(f"{exit_code}\n")
        return path

    def since(rec: Path, base: str = "base") -> tuple[str, list[str]]:
        """The selector's stdout and the flake candidates its stderr names; `why` gets stderr."""
        out = subprocess.run(
            [sys.executable, str(HERE / "affected_tests.py"), "--since", str(rec), base],
            capture_output=True,
            text=True,
            check=True,
        )
        why[:] = out.stderr.splitlines()
        prefix = "affected tests: flake candidate: "
        flakes = [
            line[len(prefix) :] for line in out.stderr.splitlines() if line.startswith(prefix)
        ]
        return out.stdout, flakes

    why: list[str] = []

    def lines(text: str) -> list[str]:
        return [line for line in text.split("\n") if line]

    def today(base: str = "base") -> str:
        out = subprocess.run(
            [sys.executable, str(HERE / "affected_tests.py"), base],
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout

    # the PR changes frob and adds gadget: today's selection is both tests that name them
    def head() -> str:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()

    run("git", "checkout", "-q", "-B", "pr", "base")
    commit({"omnigent/widgets/frob.py": "x = 10\n"})
    before_gadget = head()
    commit({"omnigent/gadget.py": "g = 1\n"})
    then = head()
    assert lines(today()) == [other, widget]
    earlier = record(then, [other, widget], {failed: True})
    # same tree, one failed node id: exactly that node id, a flake candidate
    out, flakes = since(earlier)
    assert lines(out) == [failed] and flakes == [failed], (out, flakes)
    assert f"affected tests: {failed}: failed in that run" in why, why
    # only the failed test's file changed: that file, and the node id runs with it
    commit({widget: "from omnigent.widgets import frob  # fixed\n"})
    out, flakes = since(earlier)
    assert lines(out) == [widget] and flakes == [], (out, flakes)
    # a module changed: the failed test plus what targets() picks for that module
    run("git", "reset", "-q", "--hard", then)
    commit({"omnigent/gadget.py": "g = 2\n"})
    out, flakes = since(earlier)
    assert affected_tests.targets(["omnigent/gadget.py"]) == [other]
    assert lines(out) == [other, failed] and flakes == [failed], (out, flakes)
    assert f"affected tests: {other}: names a file that differs" in why, why
    # a file added or deleted since counts as changed
    run("git", "reset", "-q", "--hard", then)
    out, _ = since(record(before_gadget, [other, widget], {failed: True}))
    assert lines(out) == [other, failed], out
    run("git", "rm", "-q", "omnigent/widgets/frob.py")
    commit({})
    out, flakes = since(earlier)
    assert lines(out) == [widget] and flakes == [], (out, flakes)
    # a collection error names the file: rerun with it, not a flake candidate
    commit({widget: "# rewritten\n"})
    out, flakes = since(record(then, [other, widget], {widget: True}))
    assert lines(out) == [widget] and flakes == [], (out, flakes)
    # a selected test file the earlier run didn't select runs
    run("git", "reset", "-q", "--hard", then)
    out, flakes = since(record(then, [widget], {failed: True}))
    assert lines(out) == [other, failed], out
    assert f"affected tests: {other}: selected now, not in that run" in why, why
    # a test setup change reruns the whole selection, so no flake is named
    commit({"tests/server/conftest.py": "# c\n"})
    out, flakes = since(earlier)
    assert lines(out) == ["tests"] and flakes == [], (out, flakes)
    # with the whole tree selected, a failed test still runs alone, unless its file is gone
    whole = record(head(), ["tests"], {failed: True, "tests/server/test_gone.py::t": True})
    out, flakes = since(whole)
    assert lines(out) == [failed] and flakes == [failed], (out, flakes)
    # a failed test gone from the tree, or outside today's selection, doesn't run
    run("git", "reset", "-q", "--hard", then)
    out, _ = since(
        record(then, [other, widget], {"tests/server/test_gone.py::t": True, third: True})
    )
    assert lines(out) == [], out
    # requeued onto a newer base: what the base moved doesn't run, the failed test does
    run("git", "checkout", "-q", "-B", "newer", "base")
    commit({"omnigent/thing.py": "t = 2\n"})
    run("git", "checkout", "-q", "pr")
    run("git", "rebase", "-q", "newer")
    assert lines(today("newer")) == [other, widget]
    out, flakes = since(earlier, "newer")
    assert lines(out) == [failed] and flakes == [failed], (out, flakes)
    # the base moved a test setup file: every selected test reruns, and no flake is named
    run("git", "checkout", "-q", "-B", "newest", "newer")
    commit({"tests/server/conftest.py": "# c\n"})
    run("git", "checkout", "-q", "-B", "pr-newest", "pr")
    run("git", "rebase", "-q", "newest")
    assert lines(today("newest")) == [other, widget]
    out, flakes = since(earlier, "newest")
    assert lines(out) == [other, widget] and flakes == [], (out, flakes)
    run("git", "checkout", "-q", "pr")
    # no usable record: today's selection, byte for byte
    unusable = [
        tmp + "/missing",
        record(then, [other, widget], {failed: True}, exit_code="124"),
        record(then, [other, widget], {failed: True}, exit_code="2"),
        record(then, [other, widget], {failed: True}, exit_code=""),
        record(then, [other, widget], [failed]),
        record(then, [other, widget], {}, exit_code="1"),  # pytest died at startup
    ]
    malformed = record(then, [other, widget], {failed: True})
    (malformed / "lastfailed.json").write_text("{not json")
    empty_tree = record(then, [other, widget], {failed: True})
    (empty_tree / "tree").write_text("")
    for rec in [*unusable, malformed, empty_tree]:
        assert since(Path(rec), "newer") == (today("newer"), []), rec
        assert any(line.startswith("affected tests: no usable record") for line in why), (rec, why)
print("affected-tests: ok")
