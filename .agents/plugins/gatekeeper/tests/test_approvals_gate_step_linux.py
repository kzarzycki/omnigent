"""approvals.py: a `gate_steps` step with `where: linux` runs only where sys.platform is Linux (#1045)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from approvals_harness import approvals, committed, gatekeeper_md, pull


@pytest.fixture
def linux_step(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A clean checkout whose policy has one step, `toolchain` (`where: linux`), a script that logs each run."""
    repo, runs = tmp_path / "repo", tmp_path / "runs.log"
    committed(repo)
    script = tmp_path / "step.py"
    script.write_text(f"open({str(runs)!r}, 'a').write('ran\\n')\n")
    command = f"{sys.executable} {script}"
    state: dict[str, Any] = {
        "policy": gatekeeper_md(
            f"loop:\n  gate_steps:\n    - name: toolchain\n      command: {command}\n      where: linux\n"
        )
    }
    monkeypatch.setattr(approvals, "project", lambda: (None, approvals.Policy(state["policy"])))
    monkeypatch.setattr(approvals, "one_repo", lambda: None)
    monkeypatch.chdir(repo)
    state["runs"] = lambda: runs.read_text().split() if runs.exists() else []
    state["set_policy"] = lambda text: state.update(policy=text)
    return state


def test_a_linux_step_runs_on_linux(
    linux_step: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    assert approvals.here(approvals.gate_plan()[0])
    assert approvals.main(["local-ci", "--record"]) == 0
    assert linux_step["runs"]() == ["ran"]


def test_a_linux_step_goes_to_left_on_darwin(
    linux_step: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    assert not approvals.here(approvals.gate_plan()[0])
    approvals.main(["local-ci", "--record"])
    assert linux_step["runs"]() == []
    assert "`where: linux`" in capsys.readouterr().out


def test_an_unknown_where_is_still_refused(linux_step: dict[str, Any]) -> None:
    linux_step["set_policy"](
        gatekeeper_md(
            "loop:\n  gate_steps:\n    - name: a\n      command: echo a\n      where: windows\n"
        )
    )
    with pytest.raises(approvals.Refused, match="must be one of any, host, linux, not 'windows'"):
        approvals.gate_plan()


def test_every_message_for_a_left_linux_step_names_linux_not_a_host(
    linux_step: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    assert approvals.main(["local-ci", "--record"]) == 0
    out = capsys.readouterr().out
    assert "`toolchain` runs only on Linux (`where: linux`), where `local-ci` runs it alone" in out
    assert "`where: host`" not in out
    assert approvals.runs_only(["toolchain"]) == "`toolchain` runs only on Linux (`where: linux`)"


def test_the_run_refusal_for_a_left_linux_step_names_linux_not_a_host(
    linux_step: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    head = approvals.git("rev-parse", "HEAD").strip()
    monkeypatch.setattr(approvals, "pull_request", lambda _n: {**pull(), "headRefOid": head})
    monkeypatch.setattr(sys, "platform", "darwin")
    with pytest.raises(approvals.Refused) as refused:
        approvals.local_ci(7, run=True)
    assert "`toolchain` runs only on Linux (`where: linux`)" in str(refused.value)
    assert "`where: host`" not in str(refused.value)
