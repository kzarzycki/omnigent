"""Env seam that titles and groups CLI-created sessions."""

from omnigent.session_seed import PROJECT_LABEL_KEY, session_seed_from_env


def test_unset_env_seeds_nothing(monkeypatch):
    """An ordinary run must not be titled or filed into a project."""
    monkeypatch.delenv("OMNIGENT_SESSION_TITLE", raising=False)
    monkeypatch.delenv("OMNIGENT_SESSION_PROJECT", raising=False)
    assert session_seed_from_env() == (None, None)


def test_empty_values_are_treated_as_unset(monkeypatch):
    """Exported-but-empty vars must not create a blank title or project."""
    monkeypatch.setenv("OMNIGENT_SESSION_TITLE", "")
    monkeypatch.setenv("OMNIGENT_SESSION_PROJECT", "")
    assert session_seed_from_env() == (None, None)


def test_project_becomes_the_sidebar_label(monkeypatch):
    """Title passes through; project rides the reserved grouping label."""
    monkeypatch.setenv("OMNIGENT_SESSION_TITLE", "audit-sync 2026-08-24")
    monkeypatch.setenv("OMNIGENT_SESSION_PROJECT", "omnigent fork sync")
    title, labels = session_seed_from_env()
    assert title == "audit-sync 2026-08-24"
    assert labels == {PROJECT_LABEL_KEY: "omnigent fork sync"}
