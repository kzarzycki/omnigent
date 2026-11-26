"""A cloud session's brief says where the pack's scripts come from: a clone of the newest release tag, resolved in the
VM at run time, or the tag the brief pins with a `Pack:` line; never the checkout's vendored copy (#1987)."""

import re
import subprocess
from pathlib import Path

import pytest
from test_omnigent_agent import omnigent_agent as oa


def cloud_brief(brief: str) -> str:
    return oa.first_message(True, False, 9, "/tmp/r.md", None, brief)


def clone_command(text: str) -> str:
    found = re.search(r"`(git clone -q --depth 1 --branch .*? \"\$HOME/pack\")`", text)
    assert found, text
    return found[1]


def test_a_cloud_brief_clones_the_latest_release_by_default() -> None:
    text = cloud_brief("Fix the bug.")
    line = clone_command(text)
    assert "git ls-remote --tags --refs" in line and "sort -V" in line and oa.PACK_URL in line
    assert not re.search(
        r"--branch engineering-v\d", line
    )  # no tag written in: resolved when it runs
    assert "never from the checkout's vendored `.agents/skills`" in text
    assert (
        "{{" not in text and "}}" not in text
    )  # the preamble's format() left no escaped braces behind


def test_a_brief_that_pins_a_release_gets_that_release() -> None:
    text = cloud_brief("Fix the bug.\nPack: engineering-v0.33.1\n")
    assert clone_command(text).startswith("git clone -q --depth 1 --branch engineering-v0.33.1 ")
    assert "ls-remote" not in text and "the release this brief pins, engineering-v0.33.1" in text


def test_a_host_brief_names_no_pack_source() -> None:
    assert "$HOME/pack" not in oa.first_message(False, False, 9, "/tmp/r.md", None, "Fix the bug.")


def test_the_latest_tag_resolves_by_version_not_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shell the line carries, run against a repo whose tags sort wrong as text (0.9 after 0.35), clones the
    highest release, and ignores tags that are no release."""
    repo = tmp_path / "pack"
    run = lambda *a: subprocess.run(a, cwd=repo, check=True, capture_output=True, text=True)
    repo.mkdir()
    run("git", "init", "-q")
    commit = ("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty")
    for tag in ("engineering-v0.9.2", "engineering-v0.35.2", "engineering-v0.10.0", "other-v9.0.0"):
        run(*commit, "-m", tag)  # one commit each, so the clone's commit names one tag
        run("git", "tag", tag)
    monkeypatch.setattr(oa, "PACK_URL", str(repo))
    command = clone_command(cloud_brief("Fix the bug.")).replace(
        '"$HOME/pack"', str(tmp_path / "out")
    )
    subprocess.run(["sh", "-c", command], check=True, capture_output=True)
    described = subprocess.run(
        ["git", "-C", str(tmp_path / "out"), "describe", "--tags", "--exact-match"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert described.stdout.strip() == "engineering-v0.35.2"
