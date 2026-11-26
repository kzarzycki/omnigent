"""omnigent_agent.py: a verifier carries the PR it reviews as the label ``omni.pr``, as a ``--pr`` worker does,
so spend groups by PR; reap holds the worker on it, never the verifier (#945)."""

from pathlib import Path

import pytest
import test_omnigent_agent as base  # the faked server; Posting's sessions come from the real create
from test_omnigent_agent import no_model, no_sleep  # noqa: F401  (the fixtures apply here by name)

omnigent_agent = base.omnigent_agent
VERIFIER = "/x/engineering-loop/verifier.md"


def labels(og: base.Posting) -> dict:
    return og.bodies[-1].get("labels", {})


def test_a_worker_keeps_its_pr_label_set_after_create(tmp_path: Path) -> None:
    og = base.Posting()
    sid, _ = omnigent_agent.start(
        og,
        "claude-cloud",
        "t",
        "do X",
        tmp_path / "r.md",
        "/x/engineering-loop/worker.md",
        0,
        pr=omnigent_agent.pr_ref("o/r#7"),
    )
    assert labels(og) == {"omni.role": "worker"} and og.labels[sid] == {"omni.pr": "o/r#7"}


def test_a_verifier_is_labelled_with_the_pr_it_reviews(tmp_path: Path) -> None:
    og = base.Posting()
    omnigent_agent.start(
        og,
        "codex",
        "t",
        "do X",
        tmp_path / "r.md",
        VERIFIER,
        0,
        about=omnigent_agent.pr_ref("o/r#9"),
    )
    assert labels(og) == {"omni.role": "verifier", "omni.pr": "o/r#9"}


def test_a_pr_whose_repository_does_not_resolve_keeps_its_number_and_the_start_goes_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_repo(*_: object) -> str:
        raise omnigent_agent.Fail("not a checkout")

    monkeypatch.setattr(omnigent_agent, "pr_repo", no_repo)
    og = base.Posting()
    omnigent_agent.start(og, "codex", "t", "do X", tmp_path / "r.md", VERIFIER, 0, about=5)
    assert labels(og) == {"omni.role": "verifier", "omni.pr": "#5"}


def test_a_session_with_no_pr_has_no_pr_label(tmp_path: Path) -> None:
    og = base.Posting()
    omnigent_agent.start(og, "codex", "t", "do X", tmp_path / "r.md", None, 0)
    assert "omni.pr" not in labels(og)


def test_reap_finishes_a_verifier_whose_pr_is_open_and_holds_the_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    og = base.FakeOmnigent()
    monkeypatch.setattr(omnigent_agent, "pr_hold", lambda repo, pr: "PR open")
    og.labels["v"] = {"omni.role": "verifier", "omni.pr": "o/r#7"}
    og.labels["w"] = {"omni.role": "worker", "omni.pr": "o/r#7"}
    og.sessions["v"] = og.sessions["w"] = []
    lines, _ = omnigent_agent.reap(og, ["v", "w"])
    assert og.archived == ["v"] and "kept (PR open)" in lines[1]


def test_reap_merged_finds_the_worker_of_a_merged_pr_never_its_verifier() -> None:
    og = base.FakeOmnigent()
    live = [
        {"id": "v", "labels": {"omni.role": "verifier", "omni.pr": "o/r#7"}},
        {"id": "w", "labels": {"omni.role": "worker", "omni.pr": "o/r#7"}},
    ]
    og.call = lambda method, path, *_: {"data": live}  # type: ignore[method-assign]
    assert omnigent_agent.pr_workers(og) == ["w"]
    assert omnigent_agent.pr_workers(og, omnigent_agent.pr_ref("o/r#7")) == ["w"]
