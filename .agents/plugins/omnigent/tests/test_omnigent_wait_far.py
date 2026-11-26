"""omnigent_agent.py wait brings a report written on the session's own host home (#995)."""

import time
from pathlib import Path

import pytest
from test_omnigent_agent import (  # noqa: F401  (the fixtures apply here by name)
    FakeOmnigent,
    no_model,
    no_sleep,
    omnigent_agent,
    this_repo,
)

REMOTE_DIR = "remote/work/tmp/loop"  # under tmp_path: a copy written to it by mistake lands there


REMOTE = ""


class Far(FakeOmnigent):
    """A session on another host, whose files are only in ``files``; ``down`` is its host
    offline, where its file API fails."""

    def __init__(self) -> None:
        super().__init__()
        self.files: dict[str, str] = {}
        self.down = False

    def info(self, sid: str) -> dict:
        return {
            **super().info(sid),
            "host_id": "mac",
            "runner_online": not self.down,
        }

    def read_file(self, sid: str, path: str) -> str:
        if self.down:
            raise omnigent_agent.Fail("GET filesystem: HTTP 502 Bad Gateway")
        if path not in self.files:
            raise omnigent_agent.Gone(path)
        return self.files[path]

    def modified(self, sid: str, path: str) -> float:
        if self.down:
            raise omnigent_agent.Fail("GET filesystem: HTTP 502 Bad Gateway")
        if path not in self.files:
            raise omnigent_agent.Gone(path)
        return time.time() - 3600


def far(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Far, str]:
    global REMOTE
    REMOTE = str(tmp_path / REMOTE_DIR / "r.md")  # absolute, on the session's host only
    og = Far()
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.chdir(tmp_path)
    return og, og.create("claude", "t")


def test_wait_on_a_report_only_on_the_remote_host_returns_its_local_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og, sid = far(monkeypatch, tmp_path)
    og.files[REMOTE] = "DONE: ok\n"
    assert not Path(REMOTE).exists() and not Path(REMOTE).parent.exists()
    assert omnigent_agent.main(["wait", sid, REMOTE, "--keep"]) == 0
    copy = Path("tmp/loop/r.md")
    assert copy.read_text() == "DONE: ok\n"
    assert not Path(REMOTE).exists()  # nothing was written at the remote path here
    assert capsys.readouterr().out.strip() == str(copy)  # a path this host reads


def test_wait_on_a_remote_question_leaves_a_local_copy_and_names_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og, sid = far(monkeypatch, tmp_path)
    og.files[REMOTE + ".question"] = "which base?\n"
    assert omnigent_agent.main(["wait", sid, REMOTE, "--keep"]) == omnigent_agent.ASKED
    assert "tmp/loop/r.md.question" in capsys.readouterr().err
    assert Path("tmp/loop/r.md.question").read_text() == "which base?\n"


def test_wait_with_the_host_offline_says_so_and_names_the_remote_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og, sid = far(monkeypatch, tmp_path)
    og.files[REMOTE] = "DONE: ok\n"
    og.down = True
    assert omnigent_agent.main(["wait", sid, REMOTE, "--keep"]) == 1
    err = capsys.readouterr().err
    assert "host is offline" in err and REMOTE in err
    assert not Path("tmp/loop/r.md").exists()


def test_a_host_that_stops_answering_mid_copy_is_named_with_the_remote_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    og, sid = far(monkeypatch, tmp_path)
    og.down = True
    og.info = lambda s: {"host_id": "mac", "runner_online": True}  # type: ignore[method-assign]
    with pytest.raises(omnigent_agent.Fail, match=f"host did not answer: {REMOTE} is on that"):
        omnigent_agent.bring_home(og, sid, Path(REMOTE))


def test_a_host_that_goes_offline_on_a_later_poll_is_named_with_the_remote_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Online at the first look, gone at the next: the file API's HTTP error is not what is shown."""
    _og, sid = far(monkeypatch, tmp_path)
    reads = {"n": 0}
    first = Far.modified

    def modified(self: Far, s: str, path: str) -> float:
        reads["n"] += 1
        if reads["n"] > 2:  # the question's and the report's look of the first poll
            self.down = True
        return first(self, s, path)

    monkeypatch.setattr(Far, "modified", modified)
    assert omnigent_agent.main(["wait", sid, REMOTE, "--keep"]) == 1
    err = capsys.readouterr().err
    assert "host is offline" in err and REMOTE in err and "HTTP 502" not in err
    assert reads["n"] > 2


def test_a_file_read_that_fails_after_the_listing_answered_names_the_remote_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The listing (its mtime) and the file are two requests: the host can drop between them."""
    og, sid = far(monkeypatch, tmp_path)
    og.files[REMOTE] = "DONE: ok\n"

    def read_file(self: Far, s: str, path: str) -> str:
        self.down = True  # the listing answered; the host went away before the read
        raise omnigent_agent.Fail("GET filesystem: HTTP 502 Bad Gateway")

    monkeypatch.setattr(Far, "read_file", read_file)
    assert omnigent_agent.main(["wait", sid, REMOTE, "--keep"]) == 1
    err = capsys.readouterr().err
    assert "host is offline" in err and REMOTE in err and "HTTP 502" not in err
    assert not Path("tmp/loop/r.md").exists()


@pytest.mark.parametrize("name", ["r.md", "r.md.question"])
def test_a_local_copy_is_never_written_through_a_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    og, sid = far(monkeypatch, tmp_path)
    og.files[REMOTE + name[4:]] = "remote text\n"
    target = tmp_path / "elsewhere.txt"
    target.write_text("mine\n")
    Path("tmp/loop").mkdir(parents=True)
    Path("tmp/loop", name).symlink_to(target)
    omnigent_agent.main(["wait", sid, REMOTE, "--keep"])
    copy = Path("tmp/loop", name)
    assert target.read_text() == "mine\n"
    assert not copy.is_symlink() and copy.read_text() == "remote text\n"
