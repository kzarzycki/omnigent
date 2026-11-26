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
# the queue's gate sets it for the whole run; each case here chooses its own mode
os.environ.pop("AFFECTED_TESTS_BUILD_INPUTS", None)
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


assert {"yaml", "pyyaml"} <= affected_tests.import_names("pyyaml") | {"pyyaml"}
assert "some_lib" in affected_tests.import_names("some-lib")  # not installed: its own name

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
    (repo / "tests" / "client").mkdir()
    (repo / "tests" / "client" / "test_uses.py").write_text(
        "from tests.server.conftest import fixture  # not conftest.py's name alone\n"
        "pytest_plugins = ['tests.e2e.conftest']\n"
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
    # a conftest selects its directory, which holds the tests that name a file too, and the
    # test files elsewhere that import it
    uses = "tests/client/test_uses.py"
    assert select(repo, {"tests/server/conftest.py": "# c\n"}) == [uses, "tests/server"]
    both = {"tests/server/conftest.py": "# c\n", "omnigent/widgets/frob.py": "x = 5\n"}
    assert select(repo, both) == [uses, "tests/server"]
    assert select(repo, {"tests/conftest.py": "# c\n"}) == ["tests"]
    assert select(repo, {"conftest.py": "# c\n"}) == ["tests"]
    # a suite check-fast skips: only the tests outside it that load it
    assert affected_tests.targets(["tests/e2e/conftest.py"]) == [uses]
    assert affected_tests.targets(["tests/gone/conftest.py"]) == []  # a deleted directory
    assert select(repo, {"pyproject.toml": "[x]\n"}) == ["tests"]
    assert select(repo, {"tests/helpers/util.py": "z = 1\n"}) == ["tests"]
    # suites the default addopts ignore are never selected
    (repo / "tests" / "e2e").mkdir()
    (repo / "tests" / "e2e" / "test_live.py").write_text("import omnigent.widgets.frob\n")
    (repo / "tests" / "server" / "test_requests.py").write_text("import requests\n")
    (repo / "tests" / "server" / "test_yaml.py").write_text("import yaml\n")
    (repo / "tests" / "server" / "test_root.py").write_text("import omnigent.widgets\n")
    lock = (
        '[[package]]\nname = "requests"\nversion = "1.0"\n\n'
        '[[package]]\nname = "other-lib"\nversion = "2.0"\n\n'
        '[[package]]\nname = "pyyaml"\nversion = "6.0"\n\n'
        '[[package]]\nname = "omnigent"\nversion = "0.1"\nsource = { editable = "." }\n'
    )
    (repo / "uv.lock").write_text(lock)
    run("git", "add", "-A")
    run("git", "commit", "-q", "-m", "e2e")
    run("git", "tag", "-f", "base")
    assert select(repo, {"omnigent/widgets/frob.py": "x = 3\n"}) == [widget]
    # the deployed pack's own tests run in the pack's repo, not here
    assert select(repo, {".agents/plugins/demo/tests/test_demo.py": "x = 1\n"}) == []
    # the queue's floor: a build input alone selects nothing the tests don't name
    floor = {"AFFECTED_TESTS_BUILD_INPUTS": "floor"}
    requests_test = "tests/server/test_requests.py"
    assert select(repo, {"pyproject.toml": "[x]\n"}, **floor) == []
    # a test that reads the file by name still runs
    run("git", "checkout", "-q", "-B", "work", "base")
    (repo / "tests" / "server" / "test_lock_format.py").write_text('read("pyproject.toml")\n')
    run("git", "add", "-A")
    run("git", "commit", "-q", "-m", "lock test")
    run("git", "tag", "-f", "base")
    lock_test = "tests/server/test_lock_format.py"
    assert select(repo, {"pyproject.toml": "[x]\n"}, **floor) == [lock_test]
    # an import after a comma, and a patched dotted name, name the package too
    run("git", "checkout", "-q", "-B", "work", "base")
    (repo / "tests" / "server" / "test_comma.py").write_text("import os, requests\n")
    (repo / "tests" / "server" / "test_patch.py").write_text('patch("requests.get")\n')
    run("git", "add", "-A")
    run("git", "commit", "-q", "-m", "comma")
    run("git", "tag", "-f", "base")
    assert select(repo, {"pyproject.toml": "[x]\n"}) == ["tests"]  # outside the queue: unchanged
    requests_importers = sorted(
        [requests_test, "tests/server/test_comma.py", "tests/server/test_patch.py"]
    )
    got = select(repo, {"uv.lock": lock.replace("1.0", "1.1")}, **floor)
    assert got == requests_importers, got
    assert select(repo, {"uv.lock": lock.replace("2.0", "2.1")}, **floor) == []
    assert (
        select(
            repo, {"uv.lock": lock + '\n[[package]]\nname = "Gizmos"\nversion = "3"\n'}, **floor
        )
        == []
    )
    yaml_test = "tests/server/test_yaml.py"
    assert select(repo, {"uv.lock": lock.replace("6.0", "6.1")}, **floor) == [yaml_test]  # alias
    # the project's own editable package moving selects nothing: every test imports it
    assert select(repo, {"uv.lock": lock.replace("0.1", "0.2")}, **floor) == []
    both_inputs = {"uv.lock": lock.replace("1.0", "1.1"), "omnigent/widgets/frob.py": "x = 6\n"}
    assert select(repo, both_inputs, **floor) == sorted([*requests_importers, widget])
    assert select(repo, {"tests/helpers/util.py": "z = 2\n"}, **floor) == [
        "tests"
    ]  # shared test setup stays whole
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
    # same tree, one failed node id: a flake candidate, so the whole selection runs, since alone a
    # test that fails only after other tests passes
    out, flakes = since(earlier)
    assert lines(out) == [other, widget] and flakes == [failed], (out, flakes)
    whole_line = (
        "affected tests: nothing changed reselects 1 failed test(s), so the whole selection runs"
    )
    assert any(line.startswith(whole_line) for line in why), why
    # only the failed test's file changed: that file, and the node id runs with it
    commit({widget: "from omnigent.widgets import frob  # fixed\n"})
    out, flakes = since(earlier)
    assert lines(out) == [widget] and flakes == [], (out, flakes)
    # two failed tests and a fix for one: the other is a candidate, so the whole selection runs
    out, flakes = since(record(then, [other, widget], {failed: True, f"{other}::t": True}))
    assert lines(out) == [other, widget] and flakes == [f"{other}::t"], (out, flakes)
    assert f"affected tests: flake candidate: {other}::t" in why, why
    # a module and the failed test's file changed: what targets() picks for each, with reasons
    run("git", "reset", "-q", "--hard", then)
    commit({"omnigent/gadget.py": "g = 2\n", third: "import omnigent.thing  # c\n"})
    out, flakes = since(record(then, [other, widget, third], {f"{third}::t": True}))
    assert affected_tests.targets(["omnigent/gadget.py"]) == [other]
    assert lines(out) == [other, third] and flakes == [], (out, flakes)
    assert f"affected tests: {other}: names a file that differs" in why, why
    assert f"affected tests: {third}: names a file that differs" in why, why
    # a change that doesn't reselect the failed test: the whole selection runs
    run("git", "reset", "-q", "--hard", then)
    commit({"omnigent/gadget.py": "g = 2\n"})
    out, flakes = since(earlier)
    assert lines(out) == [other, widget] and flakes == [failed], (out, flakes)
    # a file added or deleted since counts as changed
    run("git", "reset", "-q", "--hard", then)
    out, _ = since(record(before_gadget, [other, widget], {}, "0"))
    assert lines(out) == [other], out
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
    out, flakes = since(record(then, [widget], {}, "0"))
    assert lines(out) == [other], out
    assert f"affected tests: {other}: selected now, not in that run" in why, why
    # a test setup change reruns its whole directory and its importers, so no flake is named
    commit({"tests/server/conftest.py": "# c\n"})
    out, flakes = since(earlier)
    assert lines(out) == [uses, "tests/server"] and flakes == [], (out, flakes)
    # a whole-tree record: a flake candidate runs today's selection; a failed file that's gone
    # doesn't count
    whole = record(head(), ["tests"], {failed: True, "tests/server/test_gone.py::t": True})
    out, flakes = since(whole)
    assert lines(out) == [uses, "tests/server"] and flakes == [failed], (out, flakes)
    # a failed test gone from the tree, or outside today's selection, doesn't run
    run("git", "reset", "-q", "--hard", then)
    out, _ = since(
        record(then, [other, widget], {"tests/server/test_gone.py::t": True, third: True})
    )
    assert lines(out) == [], out
    # requeued onto a newer base: what the base moved isn't a change, so the failed test is a flake
    # candidate and the whole selection runs
    run("git", "checkout", "-q", "-B", "newer", "base")
    commit({"omnigent/thing.py": "t = 2\n"})
    run("git", "checkout", "-q", "pr")
    run("git", "rebase", "-q", "newer")
    assert lines(today("newer")) == [other, widget]
    out, flakes = since(earlier, "newer")
    assert lines(out) == [other, widget] and flakes == [failed], (out, flakes)
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
    # floor mode and --since: a package that moved after the record's run reselects its importers
    run("git", "checkout", "-q", "-B", "pr", "base")
    then = head()
    requests_test = "tests/server/test_requests.py"
    floor_record = record(then, [requests_test, widget], {}, exit_code="0")
    commit({"uv.lock": lock.replace("1.0", "1.1")})
    out = subprocess.run(
        [sys.executable, str(HERE / "affected_tests.py"), "--since", str(floor_record), "base"],
        env={**os.environ, "AFFECTED_TESTS_BUILD_INPUTS": "floor"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert lines(out.stdout) == requests_importers, out
print("affected-tests: ok")
