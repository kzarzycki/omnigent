"""One running worker or verifier per PR (waste audit, finding 9): `start` refuses a second beside a running one, and
`reap --force` on the one it names stops it, so the start can go on."""

from pathlib import Path

import pytest
import test_omnigent_agent as base
from test_omnigent_agent import no_model, no_sleep  # noqa: F401  (the fixtures apply here by name)

omnigent_agent = base.omnigent_agent
WORKER, VERIFIER = "/x/engineering-loop/worker.md", "/x/engineering-loop/verifier.md"


def listing(og, *sessions: dict) -> None:
    """The server's session list holds ``sessions`` beside whatever ``og`` would answer otherwise."""
    real = og.call

    def call(method: str, path: str, *a):
        if method == "GET" and path.startswith("/v1/sessions?kind=any"):
            return {"data": list(sessions)}
        return real(method, path, *a)

    og.call = call


def twin(role: str, pr: str = "o/r#7", **extra) -> dict:
    return {
        "id": "first",
        "status": "running",
        "labels": {"omni.role": role, "omni.pr": pr},
        **extra,
    }


@pytest.mark.parametrize(
    ("role", "kwargs"),
    [
        (WORKER, {"pr": omnigent_agent.pr_ref("o/r#7")}),
        (VERIFIER, {"about": omnigent_agent.pr_ref("o/r#7")}),
    ],
)
def test_a_second_session_of_a_pr_and_role_is_refused_while_the_first_runs(
    tmp_path: Path, role, kwargs
) -> None:
    og = base.Posting()
    listing(og, twin(Path(role).stem))
    with pytest.raises(
        omnigent_agent.Fail, match=r"PR o/r#7 already has a running .*first.*reap --force first"
    ):
        omnigent_agent.start(og, "claude-cloud", "t", "do X", tmp_path / "r.md", role, 0, **kwargs)
    assert not og.bodies  # no session was made


@pytest.mark.parametrize(
    "other",
    [
        twin("worker", status="idle"),  # a kept --pr worker between rounds: start --pr replaces it
        twin("worker", archived=True),
        twin("verifier"),  # another role
        twin("worker", pr="o/r#8"),  # another PR
    ],
)
def test_a_start_goes_on_beside_no_running_twin(tmp_path: Path, other) -> None:
    og = base.Posting()
    listing(og, other)
    sid, _ = omnigent_agent.start(
        og,
        "claude-cloud",
        "t",
        "do X",
        tmp_path / "r.md",
        WORKER,
        0,
        pr=omnigent_agent.pr_ref("o/r#7"),
    )
    assert sid and og.bodies


def test_reap_force_stops_a_named_running_worker_whose_pr_is_open(monkeypatch) -> None:
    og = base.FakeOmnigent()
    monkeypatch.setattr(omnigent_agent, "pr_hold", lambda repo, pr: "o/r#7 is open")
    og.labels["first"] = {"omni.role": "worker", "omni.pr": "o/r#7"}
    og.sessions["first"] = []
    og.info = lambda sid: {"id": sid, "status": "running", "labels": og.labels[sid]}  # type: ignore[method-assign]
    lines, failed = omnigent_agent.reap(og, ["first"], force=True)
    assert not failed and og.interrupted == ["first"] and og.archived == ["first"]
    assert "archived (stopped)" in lines[0]


def test_reap_force_keeps_an_idle_kept_worker_and_reap_alone_a_running_one(monkeypatch) -> None:
    og = base.FakeOmnigent()
    monkeypatch.setattr(omnigent_agent, "pr_hold", lambda repo, pr: "o/r#7 is open")
    og.labels["first"] = {"omni.role": "worker", "omni.pr": "o/r#7"}
    og.sessions["first"] = []
    status = {"first": "idle"}
    og.info = lambda sid: {"id": sid, "status": status[sid], "labels": og.labels[sid]}  # type: ignore[method-assign]
    assert "kept (o/r#7 is open)" in omnigent_agent.reap(og, ["first"], force=True)[0][0]
    status["first"] = "running"
    assert "kept" in omnigent_agent.reap(og, ["first"])[0][0]
    assert og.archived == [] and og.interrupted == []


def running(og, sid: str, labels: dict) -> None:
    """``sid``, running and not stuck (no dated item), with ``labels``."""
    og.labels[sid] = labels
    og.sessions[sid] = []
    infos = getattr(og, "_infos", {})
    infos[sid] = {"id": sid, "status": "running", "labels": labels}
    og._infos = infos
    og.info = lambda s: dict(og._infos[s])  # type: ignore[method-assign]


@pytest.mark.parametrize("how", ["children", "merged"])
def test_reap_force_keeps_a_running_pr_worker_it_reached_without_naming(monkeypatch, how) -> None:
    """Only a session named on the command line is stopped: one reached through `--children` or `--merged` keeps
    today's rules, so a running worker of an open PR stays."""
    og = base.FakeOmnigent()
    monkeypatch.setattr(omnigent_agent, "pr_hold", lambda repo, pr: "o/r#7 is open")
    running(og, "w", {"omni.role": "worker", "omni.pr": "o/r#7"})
    og.call = lambda method, path, *a: {"data": [dict(og._infos["w"])]}  # type: ignore[method-assign]
    if how == "children":
        og.kids = ["w"]
        lines, _ = omnigent_agent.reap(og, [], parent="lead", force=True)
    else:
        lines, _ = omnigent_agent.reap(og, [], force=True, merged=True, pr=7)
    assert (
        og.interrupted == [] and og.archived == [] and any("w" in l and "kept" in l for l in lines)
    )


@pytest.mark.parametrize(
    "labels",
    [
        {},  # no PR, no role
        {"omni.pr": "o/r#7"},  # a PR label but no role
        {"omni.role": "lead", "omni.pr": "o/r#7"},  # a role outside the worker and verifier
        {"omni.role": "worker"},  # a role but no PR
    ],
)
def test_reap_force_keeps_a_named_running_session_that_is_no_role_session_on_a_pr(
    monkeypatch, labels
) -> None:
    og = base.FakeOmnigent()
    monkeypatch.setattr(omnigent_agent, "pr_hold", lambda repo, pr: "o/r#7 is open")
    running(og, "s", labels)
    lines, _ = omnigent_agent.reap(og, ["s"], force=True)
    assert og.interrupted == [] and og.archived == [] and "kept" in lines[0]


def test_a_lead_with_a_pr_starts_beside_a_running_lead_of_that_pr(tmp_path: Path) -> None:
    """Only a worker or a verifier repeats a PR's run: a lead coordinates, so a second one is no twin."""
    og = base.Posting()
    listing(og, twin("lead"))
    sid, _ = omnigent_agent.start(
        og,
        "claude-cloud",
        "t",
        "do X",
        tmp_path / "r.md",
        "/x/engineering-loop/lead.md",
        0,
        pr=omnigent_agent.pr_ref("o/r#7"),
    )
    assert sid and og.bodies
