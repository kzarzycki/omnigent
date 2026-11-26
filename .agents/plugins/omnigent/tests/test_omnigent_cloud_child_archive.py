"""omnigent_agent.py: a cloud child's taken result archives it, background work and all (#2052).

Whatever a finished cloud worker, verifier or gate run leaves running in its VM keeps spending the account's
GitHub quota, so ``finish_delivered`` no longer keeps such a session for its background tasks. A lead, a manager,
a ``--keep`` child, a blind reap and every host session keep today's holds."""

from pathlib import Path

import pytest
import test_omnigent_agent as base
from test_omnigent_agent import no_model, no_sleep  # noqa: F401  (the fixtures apply here by name)

omnigent_agent = base.omnigent_agent
TASK = {"type": "shell", "status": "running"}


@pytest.fixture
def og(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> base.CloudFake:
    fake = base.CloudFake(reply=base.CLOUD_REPORT)
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: fake)
    monkeypatch.chdir(tmp_path)
    return fake


def cloud(og: base.FakeOmnigent, tmp_path: Path, role: str = "", parent: bool = True) -> str:
    sid, _ = omnigent_agent.start(og, "claude-cloud", "t", "do X", tmp_path / "r", None, 0)
    if role:
        og.labels[sid] = {"omni.role": role}
    if parent:
        og.parents[sid] = "p0"
    og.tasks = [TASK]  # a poller it left running
    return sid


@pytest.mark.parametrize("role", ["worker", "verifier", ""])
def test_a_taken_cloud_child_is_archived_with_its_background_work(
    og: base.CloudFake, tmp_path: Path, role: str
) -> None:
    sid = cloud(og, tmp_path, role)
    line, failed = omnigent_agent.finish_delivered(og, sid)  # the supervisor's relay of a DONE
    assert not failed and og.archived == [sid]
    assert line.endswith("archived (idle; 1 background task(s) ended with its VM)")


def test_wait_archives_a_cloud_child_whose_vm_still_runs_a_task(
    og: base.CloudFake, tmp_path: Path
) -> None:
    sid = cloud(og, tmp_path, "worker")
    assert omnigent_agent.main(["wait", sid, "r.md"]) == 0
    assert og.archived == [sid]


@pytest.mark.parametrize("how", ["lead", "manager", "top-level", "keep", "label", "reap", "host"])
def test_what_stays_open_with_its_background_work(
    og: base.CloudFake, tmp_path: Path, how: str
) -> None:
    role = {"lead": "lead", "manager": "cos"}.get(how, "worker" if how != "top-level" else "")
    sid = cloud(og, tmp_path, role, parent=how != "top-level")
    if how == "label":
        og.labels[sid][omnigent_agent.KEEP_LABEL] = "1"
    if how == "host":
        og.harnesses = {"claude-cloud": "acp:claude"}  # the same session on a host harness
    line, _ = omnigent_agent.finish_delivered(og, sid, keep=how == "keep", settled=how != "reap")
    assert og.archived == [] and "kept (" in line


def test_an_open_card_or_prompt_still_holds_a_cloud_child(
    og: base.CloudFake, tmp_path: Path
) -> None:
    sid = cloud(og, tmp_path, "worker")
    og.prompts = [{"elicitation_id": "question_1", "params": {"message": "Land it?"}}]
    assert "kept (card open" in omnigent_agent.finish_delivered(og, sid)[0]
    og.prompts = [{"elicitation_id": "elicit_approval_1", "params": {"message": "Bash?"}}]
    assert "kept (blocked on a prompt)" in omnigent_agent.finish_delivered(og, sid)[0]
    assert og.archived == []
