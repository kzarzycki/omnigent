"""omnigent_agent.py: the chief of staff's console follows a lead no registry holds, and ``start --top-level``
refuses a lead with no registry (#2181)."""

import json
from pathlib import Path

import pytest
import test_omnigent_agent as base  # the faked server
from test_omnigent_agent import no_model, no_sleep  # noqa: F401  (the fixtures apply here by name)

omnigent_agent = base.omnigent_agent

STRAY = "a" * 32  # a live top-level lead started with no registry


def lead_row(sid: str, **extra: object) -> dict:
    return {"id": sid, "title": f"t-{sid[:4]}", "labels": {"omni.role": "lead"}, **extra}


def adopted(registry: Path) -> list[dict]:
    path = registry.with_suffix(".events.jsonl")
    lines = path.read_text().splitlines() if path.exists() else []
    return [e for e in map(json.loads, lines) if e.get("kind") == "adopted"]


def test_the_console_adopts_a_live_lead_on_no_registry_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = tmp_path / "leads.registry"
    registry.write_text("")
    og = base.Leads(**{STRAY: [base.told("brief", 60)]})
    og.rows = [lead_row(STRAY)]
    base.supervised(og, registry, monkeypatch, 2, console=True)
    lines = registry.read_text().splitlines()
    assert len(lines) == 1 and lines[0].startswith(f"{STRAY}=")
    assert omnigent_agent.entry(lines[0])[4] is True  # followed
    [event] = adopted(registry)
    assert event["session"] == STRAY and "a lead on no registry" in event["line"]
    assert STRAY in og.reads  # followed in the poll that adopted it


def test_the_console_adopts_no_lead_someone_already_watches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = tmp_path / "leads.registry"
    followed, archived, child, beside, helper, worker = (c * 32 for c in "bcdefg")
    registry.write_text(f"{followed}={tmp_path / 'b.md'}\tfollow\n")
    (tmp_path / "app-leads.registry").write_text(f"{beside}={tmp_path / 'e.md'}\tfollow\n")
    og = base.Leads(**{followed: [base.told("brief", 60)]})
    og.rows = [
        lead_row(followed),
        lead_row(archived, archived=True),
        lead_row(child, parent_session_id=followed),
        lead_row(beside),  # a manager's lead, on the registry beside
        {**lead_row(helper), "labels": {"omni.role": "lead", "omni.created_by": followed}},
        {"id": worker, "labels": {"omni.role": "worker"}},
    ]
    base.supervised(og, registry, monkeypatch, 2, console=True)
    assert registry.read_text() == f"{followed}={tmp_path / 'b.md'}\tfollow\n"
    assert adopted(registry) == []


def test_a_lead_on_a_leads_registry_elsewhere_is_not_adopted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manager's own registry may sit in its repo, not beside the console's: its ``leads=``
    field on the console's line names it."""
    manager, theirs = "b" * 32, "e" * 32
    elsewhere = tmp_path / "repo" / "tmp" / "loop" / "app-leads.registry"
    elsewhere.parent.mkdir(parents=True)
    elsewhere.write_text(f"{theirs}={tmp_path / 'e.md'}\tfollow\n")
    console = tmp_path / "cos"
    console.mkdir()
    registry = console / "leads.registry"
    line = f"{manager}={tmp_path / 'b.md'}\tfollow\tleads={elsewhere}\n"
    registry.write_text(line)
    og = base.Leads(**{manager: [base.told("brief", 60)], theirs: [base.told("brief", 60)]})
    og.rows = [lead_row(manager), lead_row(theirs)]
    base.supervised(og, registry, monkeypatch, 2, console=True)
    assert theirs not in registry.read_text() and adopted(registry) == []


def test_a_lead_archived_since_the_cached_listing_is_not_adopted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The listing is cached for 30 s, and an archive drops the lead's line: the stale row alone
    would adopt it again."""
    registry = tmp_path / "leads.registry"
    registry.write_text("")
    og = base.Leads(**{STRAY: [base.told("brief", 60)]})
    og.rows = [lead_row(STRAY)]  # the cached row still reads live
    og.snaps[STRAY] = {"archived": True}
    base.supervised(og, registry, monkeypatch, 2, console=True)
    assert registry.read_text() == "" and adopted(registry) == []


def test_a_manager_console_adopts_no_lead(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry = tmp_path / "app-leads.registry"
    registry.write_text("")
    og = base.Leads(**{STRAY: [base.told("brief", 60)]})
    og.rows = [lead_row(STRAY)]
    base.supervised(og, registry, monkeypatch, 2, console=True)
    assert registry.read_text() == "" and adopted(registry) == []


def test_a_top_level_lead_start_with_no_registry_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    og = base.FakeOmnigent()
    monkeypatch.setattr(omnigent_agent, "Omnigent", lambda: og)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OMNIGENT_SUPERVISE_REGISTRY", raising=False)
    brief = tmp_path / "b.md"
    brief.write_text("do X")
    args = [
        "start",
        "claude",
        "t",
        str(brief),
        str(tmp_path / "a.md"),
        "--top-level",
        "--role",
        "lead",
    ]
    assert omnigent_agent.main(args) == 2
    assert "--registry or $OMNIGENT_SUPERVISE_REGISTRY" in capsys.readouterr().err
    assert og.top_level == []  # no session was created
    registry = tmp_path / "leads.registry"
    monkeypatch.setenv("OMNIGENT_SUPERVISE_REGISTRY", str(registry))
    assert omnigent_agent.main(args) == 0
    assert omnigent_agent.entry(registry.read_text().strip())[4] is True


def test_a_fresh_lead_is_adopted_only_after_the_grace_period_2313(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A launcher writes its lead to its own supervisor's registry a moment after the session exists: inside the
    grace window the console leaves it be, after it the console adopts a lead no registry holds."""
    import time

    registry = tmp_path / "leads.registry"
    registry.write_text("")
    og = base.Leads(**{STRAY: [base.told("brief", 60)]})
    og.rows = [lead_row(STRAY, created_at=time.time() - 5)]
    base.supervised(og, registry, monkeypatch, 2, console=True)
    assert registry.read_text() == "" and adopted(registry) == []  # young: not adopted
    og.rows = [lead_row(STRAY, created_at=time.time() - omnigent_agent.ADOPT_GRACE_SECONDS - 5)]
    base.supervised(og, registry, monkeypatch, 2, console=True)
    assert registry.read_text().startswith(f"{STRAY}=") and len(adopted(registry)) == 1
