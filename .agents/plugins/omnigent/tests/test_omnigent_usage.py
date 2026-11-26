"""omnigent_agent.py ``usage``: a lead's session tree's tokens and cost for its PR body (#1127)."""

from pathlib import Path

import pytest
import test_omnigent_agent as base  # the module under test, its fixtures
from test_omnigent_agent import no_model, no_sleep  # noqa: F401  (the fixtures apply here by name)

omnigent_agent = base.omnigent_agent


def record(tokens: int, usd: float, root: str = "", title: str = "") -> dict:
    """A session record as ``GET /v1/sessions/<id>`` gives it: usage totals its whole tree."""
    found = {"usage_by_model": {"m": {"total_tokens": tokens, "total_cost_usd": usd}}}
    found["total_cost_usd"] = usd
    return {**found, **({"root_conversation_id": root} if root else {}), "title": title}


class Records:
    """Session records by id; an id missing from ``records`` is not read."""

    def __init__(self, records: dict[str, dict]) -> None:
        self.records, self.reads = records, []

    def info(self, sid: str, usage: bool = False) -> dict:
        assert usage, "usage reads the full record: the slim poll record carries no usage"
        self.reads.append(sid)
        if sid not in self.records:
            raise omnigent_agent.Fail(f"GET /v1/sessions/{sid}: HTTP 404")
        return self.records[sid]

    def session_id(self, sid: str) -> str:
        return sid


def lead_tree() -> Records:
    """A lead whose record totals its tree (its worker inside), a verifier started top-level on
    another host, and a cloud session Omnigent recorded no usage for."""
    return Records(
        {
            "lead": record(2_500_000, 1.75, "lead", "lead"),
            "worker": record(500_000, 0.25, "lead"),
            "far": record(300_000, 0.75, "far"),
            "cloud": {"title": "cloud worker", "usage_by_model": {}, "total_cost_usd": 0},
        }
    )


def test_a_lead_s_tree_and_its_top_level_helpers_are_each_counted_once() -> None:
    og = lead_tree()
    lines = omnigent_agent.usage_lines(og, ["lead", "far", "worker", "cloud", "far"])
    assert lines == [
        "lead lead: 2.5M tokens, $1.75",
        "far: 0.3M tokens, $0.75",
        "worker: in lead's tree, counted there",
        "cloud cloud worker: uncounted (no usage recorded)",
        "Tokens spent: 2.8M, $2.50 (2 session trees; uncounted: cloud)",
    ]
    assert og.reads == ["lead", "far", "worker", "cloud"]  # each record read once


def test_a_session_not_read_is_uncounted_naming_why() -> None:
    og = Records({"lead": record(50_000, 0.1)})
    assert omnigent_agent.usage_lines(og, ["lead", "gone"]) == [
        "lead: 50k tokens, $0.10",
        "gone: uncounted (not read: GET /v1/sessions/gone: HTTP 404)",
        "Tokens spent: 50k, $0.10 (1 session tree; uncounted: gone)",
    ]


def test_the_command_adds_the_session_it_runs_in(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og = lead_tree()
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda *a: og)
    monkeypatch.setenv("OMNIGENT_SESSION_ID", "lead")
    assert omnigent_agent.main(["usage", "far"]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "Tokens spent: 2.8M, $2.50 (2 session trees)"


def test_the_lead_writes_its_estimate_on_the_draft_pr_and_the_actual_at_landing() -> None:
    lead = (
        Path(__file__).parents[2] / "loop" / "skills" / "engineering-loop" / "lead.md"
    ).read_text()
    lead = " ".join(lead.split())
    build, land = lead.index("3. **Branch and notes.**"), lead.index("8. **Land**")
    assert build < lead.index("`Token estimate: ~<N> (<basis>)`") < lead.index("4. **Build.**")
    spent = lead.index("`Tokens spent:` line")
    assert land < spent < lead.index("9. **Accept.**")
    assert "omnigent_agent.py usage <session>..." in lead[spent:]
    assert "model and session id of each agent" in lead


def test_a_poll_reads_the_slim_record_and_usage_the_full_one(monkeypatch) -> None:
    paths: list[str] = []
    monkeypatch.setattr(
        omnigent_agent.Omnigent, "call", lambda self, m, p, b=None: paths.append(p) or {}
    )
    og = object.__new__(omnigent_agent.Omnigent)
    og.info("s1")
    og.info("s1", usage=True)
    assert paths == [
        "/v1/sessions/s1?include_items=false&include_usage=false",
        "/v1/sessions/s1",
    ]
