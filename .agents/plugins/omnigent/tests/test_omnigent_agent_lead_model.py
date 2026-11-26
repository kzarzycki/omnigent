"""`start --top-level` reads loop.md's `Lead model:` line and routes the lead's model by it (#1085)."""

import subprocess
from pathlib import Path

import pytest
from test_omnigent_agent import (  # noqa: F401  (the autouse fixtures apply by import)
    FakeOmnigent,
    no_model,
    no_sleep,
    omnigent_agent,
    this_repo,
    trusted_folders,
)

LINE = "Lead model: mid for size:S or smaller"


def fake() -> FakeOmnigent:
    og = FakeOmnigent()
    og.listed = ["fable", "opus", "sonnet", "haiku"]
    return og


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "docs" / "agents").mkdir(parents=True)
    return root


@pytest.fixture
def labels(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    """gh stubbed: the labels of each issue number, and the number of reads."""
    table: dict[str, list[str]] = {}

    def gh(*args: str, **_k: object) -> str:
        assert args[0] == "api"
        return "\n".join(table.get(args[1].rsplit("/", 1)[1], []))

    monkeypatch.setattr(omnigent_agent, "gh", gh)
    return table


def lead(tmp_path: Path, repo: Path, line: str | None, brief: str, **kw: object) -> str | None:
    (repo / "docs" / "agents" / "loop.md").write_text(f"# Loop\n\n{line or ''}\n")
    og = fake()
    sid, _ = omnigent_agent.start(
        og, "claude", "t", brief, tmp_path / "r.md", None, 0,
        top_level=True, workspace=str(repo), **kw,
    )  # fmt: skip
    return og.overrides[sid]


def test_a_size_s_issue_sends_the_mid_model_and_a_size_m_the_default(
    tmp_path: Path, repo: Path, labels: dict[str, list[str]]
) -> None:
    labels.update({"1": ["size:S"], "2": ["size:M"], "3": ["bug"], "4": ["size:XS"]})
    assert lead(tmp_path, repo, LINE, "build #1") == "sonnet"
    assert lead(tmp_path, repo, LINE, "build #4") == "sonnet"
    assert lead(tmp_path, repo, LINE, "see https://github.com/o/r/issues/1") == "sonnet"
    assert lead(tmp_path, repo, LINE, "build #2") is None
    assert lead(tmp_path, repo, LINE, "build #3") is None  # no size label
    assert lead(tmp_path, repo, LINE, "build it") is None  # no issue named
    assert lead(tmp_path, repo, LINE, "build #1 and #2") is None  # one fails
    assert lead(tmp_path, repo, LINE, "build #1, PR #3") == "sonnet"  # #3 says nothing


def test_model_flag_and_env_override_the_line(
    tmp_path: Path, repo: Path, labels: dict[str, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    labels["1"] = ["size:S"]
    assert lead(tmp_path, repo, LINE, "build #1", model="opus") == "opus"
    monkeypatch.setenv("OMNIGENT_MODEL_CLAUDE", "haiku")
    assert lead(tmp_path, repo, LINE, "build #1") == "haiku"


def test_without_the_line_or_for_a_child_the_request_is_unchanged(
    tmp_path: Path, repo: Path, labels: dict[str, list[str]]
) -> None:
    labels["1"] = ["size:S"]
    assert lead(tmp_path, repo, None, "build #1") is None
    (repo / "docs" / "agents" / "loop.md").write_text(LINE + "\n")
    og = fake()
    sid, _ = omnigent_agent.start(
        og, "claude", "t", "build #1", tmp_path / "r.md", None, 0, workspace=str(repo)
    )
    assert og.overrides[sid] is None  # a child, not a lead


@pytest.mark.parametrize(
    ("line", "label", "tier"),
    [
        ("Lead model: top for size:M or smaller", "size:M", "opus"),
        ("Lead model: small for size:XL or smaller", "size:XL", "haiku"),
        ("Lead model: frontier for risk:high", "risk:high", "fable"),
        ("Lead model: top for risk:high", "bug", None),
    ],
)
def test_tiers_resolve_to_aliases_and_a_bare_label_matches(
    tmp_path: Path,
    repo: Path,
    labels: dict[str, list[str]],
    line: str,
    label: str,
    tier: str | None,
) -> None:
    labels["1"] = [label]
    assert lead(tmp_path, repo, line, "build #1") == tier


@pytest.mark.parametrize(
    "line",
    [
        "Lead model: gold for size:S or smaller",
        "Lead model: mid",
        "Lead model: mid for size:Q or smaller",
        "Lead model: mid for size:S or larger",
        "Lead model: mid for not risk:high",
    ],
)
def test_an_unparsable_line_fails_before_any_session_exists(
    tmp_path: Path, repo: Path, labels: dict[str, list[str]], line: str
) -> None:
    og = fake()
    (repo / "docs" / "agents" / "loop.md").write_text(line + "\n")
    with pytest.raises(omnigent_agent.Fail, match="Lead model"):
        omnigent_agent.start(
            og, "claude", "t", "build #1", tmp_path / "r.md", None, 0,
            top_level=True, workspace=str(repo),
        )  # fmt: skip
    assert og.sessions == {}


def test_the_line_is_read_through_project_line_once(
    tmp_path: Path, repo: Path, labels: dict[str, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    labels["1"] = ["size:S"]
    keys: list[str] = []
    real = omnigent_agent.project_line
    monkeypatch.setattr(
        omnigent_agent, "project_line", lambda w, k, *a: keys.append(k) or real(w, k, *a)
    )
    lead(tmp_path, repo, LINE, "build #1")
    assert keys.count("Lead model") == 1


def test_a_bare_label_needs_one_issue_to_carry_it_and_a_codex_lead_is_unchanged(
    tmp_path: Path, repo: Path, labels: dict[str, list[str]]
) -> None:
    labels.update({"1": ["risk:high"], "3": ["bug"]})
    line = "Lead model: top for risk:high"
    assert lead(tmp_path, repo, line, "build #1 and #3") == "opus"
    (repo / "docs" / "agents" / "loop.md").write_text(line + "\n")
    og = fake()
    sid, _ = omnigent_agent.start(
        og, "codex", "t", "build #1", tmp_path / "r.md", None, 0, top_level=True, workspace=str(repo)
    )  # fmt: skip
    assert og.overrides[sid] is None  # the tiers are Claude's aliases


def test_a_remote_lead_reads_the_line_on_its_own_host(
    tmp_path: Path, labels: dict[str, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    labels["1"] = ["size:S"]
    slugs: list[object] = []
    monkeypatch.setattr(omnigent_agent, "repo_slug", lambda cwd=None: slugs.append(cwd) or "o/r")

    class Remote(FakeOmnigent):
        def loop_text(self, workspace: str, host_id: str) -> str:
            return LINE + "\n" if host_id == "far" else ""

    og = Remote()
    og.listed = ["fable", "opus", "sonnet", "haiku"]
    og.host_names = {"far": "far"}
    sid, _ = omnigent_agent.start(
        og, "claude", "t", "build #1", tmp_path / "r.md", None, 0,
        top_level=True, workspace=str(tmp_path / "remote"), host_id="far",
    )  # fmt: skip
    assert og.overrides[sid] == "sonnet"  # the caller's side has no loop.md
    assert slugs == [None]  # a bare #N resolves against the caller's repo, not a remote path


@pytest.mark.parametrize(
    ("line", "names"),
    [
        ("Lead model: mid for wanted", ["help wanted"]),  # one word of a label is not the label
        (LINE, ["size:XXL"]),  # a size outside XS..XL says nothing
        (LINE, ["size:s"]),
    ],
)
def test_a_label_matches_whole_and_an_unknown_size_says_nothing(
    tmp_path: Path, repo: Path, labels: dict[str, list[str]], line: str, names: list[str]
) -> None:
    labels["1"] = names
    assert lead(tmp_path, repo, line, "build #1") is None
