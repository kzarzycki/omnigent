"""approvals.py: risk is a rating, and gatekeeper.md's `levels` says what each level asks of the spec and the merge
(#1154). Without `levels` the defaults reproduce the rule before it, which the rest of the suite holds."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from approvals_harness import (
    HEAD,
    approvals,
    approved,
    by,
    gating,  # noqa: F401
    issue,
    judged_pr,
    judging,
    lands,  # noqa: F401
    loop_md,
    note,
    pull,
    record,
    specced,
    status,
)

Gate = tuple[dict[str, Any], list[tuple[int, str]]]

GO_ON_MAIN = (
    f"PR #7 waits for the owner's go on its head (`approve merge 7 --head {HEAD} --by owner`; into main"
    " `approved:merge` is no go; `loop.delegate: all` in the policy file lets a delegate's go count), since it is "
)
DELEGATED_GO_ON_MAIN = (
    f"PR #7 waits for the owner's go on its head (`approve merge 7 --head {HEAD} --by owner`, or the delegate's:"
    f" `approve merge 7 --head {HEAD} --by delegate`; into main `approved:merge` is no go), since it is "
)
OWNER_ONLY = (
    f"PR #7 waits for the owner's go on its head (`approve merge 7 --head {HEAD} --by owner`; the delegate's go and"
    " `approved:merge` are no go), since "
)
DELEGATE_GO = f"The delegate's go on {HEAD[:12]}, under the owner's delegation"
DELEGATE_RECORD = (
    f"Approved under the owner's delegation.\nApproved: merge\nBy: delegate\nHead: {HEAD}\n"
)


def policy(settings: str) -> None:
    """The checkout's policy: `settings`, YAML."""
    Path("docs/agents/loop.md").write_text(loop_md(settings))


def check_merge(capsys: pytest.CaptureFixture[str]) -> tuple[int, list[str], list[str]]:
    """`check merge 7`: its exit, its stdout lines and its stderr lines."""
    code = approvals.main(["check", "merge", "7"])
    out, err = capsys.readouterr()
    return code, out.splitlines(), err.splitlines()


def given_go(pr: dict[str, Any], hub: dict[str, list[dict[str, Any]]]) -> None:
    """The owner's go on the PR's head, as `approve merge` leaves it."""
    pr["comments"]["nodes"].append(note(record("merge", "owner", head=pr["headRefOid"])))
    hub.setdefault(pr["headRefOid"], []).insert(
        0, status("owner", f"The owner's go on {HEAD[:12]}")
    )


def authored(pr: dict[str, Any]) -> dict[str, Any]:
    """`pr` whose issue a writer opened, so `spec.auto: true` approves its spec by policy."""
    for found in pr["closingIssuesReferences"]["nodes"]:
        found["author"] = {"login": "worker"}
    return pr


# --- the merge action ---


def test_high_risk_lands_on_the_gates_where_its_level_says_so_and_the_rating_still_shows(
    gating: Gate, capsys: pytest.CaptureFixture[str]
) -> None:
    """`levels: {high: {merge: gates}}`: a `risk:high` PR with a green gate and a satisfied default-tier verdict
    merges with no go, and the rating is printed beside the action."""
    state, _ = gating
    policy("risk:\n  verifier: true\nlevels:\n  high:\n    merge: gates\n")
    state["pull"] = judged_pr(judging("low — fine"), labels=("risk:high",))
    code, out, err = check_merge(capsys)
    assert (code, out) == (0, [])
    assert "PR #7: risk: high (`risk:high` is on PR #7); merge: gates" in err
    # the same PR under the defaults waits for the go
    policy("risk:\n  verifier: true\n")
    state["pull"] = judged_pr(judging("low — fine"), labels=("risk:high",))
    code, out, err = check_merge(capsys)
    assert (code, out) == (approvals.WAITING, [GO_ON_MAIN + "high risk: `risk:high` is on PR #7"])
    assert "PR #7: risk: high (`risk:high` is on PR #7); merge: go" in err


def test_a_high_level_on_the_gates_still_flags_a_rule_match_and_asks_a_default_tier_verdict(
    gating: Gate, capsys: pytest.CaptureFixture[str], added: list[tuple[int, str, bool]]
) -> None:
    """The rating stays tied to the review tier and the label, whatever the merge action says."""
    state, _ = gating
    policy("risk:\n  paths:\n    - app.py\nlevels:\n  high:\n    merge: gates\n")
    state["pull"] = judged_pr(judging("low — fine"))
    code, out, err = check_merge(capsys)
    assert code == 0
    assert (7, "risk:high", True) in added
    assert "PR #7: risk: high (it matches a risk rule (path `app.py`)); merge: gates" in err
    lower = judging("low — fine", heading="Verifier (codex/terra-6.1/med mid), pass 1")
    state["pull"] = judged_pr(lower)
    code, out, _ = check_merge(capsys)
    assert code == 1
    assert any("its newest verdict came from a lower tier (mid)" in line for line in out)


def test_a_low_change_waits_for_a_go_where_the_low_level_asks_one(
    gating: Gate, capsys: pytest.CaptureFixture[str], hub: dict[str, list[dict[str, Any]]]
) -> None:
    state, _ = gating
    policy("risk:\n  verifier: true\nlevels:\n  low:\n    merge: go\n")
    state["pull"] = judged_pr(judging("low — fine"))
    code, out, err = check_merge(capsys)
    assert (code, out) == (
        approvals.WAITING,
        [GO_ON_MAIN + "low risk: nothing rates it above low"],
    )
    assert "PR #7: risk: low; merge: go" in err
    given_go(state["pull"], hub)
    code, out, _ = check_merge(capsys)
    assert (code, out) == (0, [f"PR #7: the go on {HEAD} is the owner's"])


def test_a_low_level_asking_the_owners_own_go_refuses_the_delegates(
    gating: Gate,
    capsys: pytest.CaptureFixture[str],
    hub: dict[str, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With `owner` asked of a level other than `critical`, a delegate's go is refused on the rating, which reads the
    PR's issues itself."""
    state, _ = gating
    policy("loop:\n  delegate: all\nlevels:\n  low:\n    merge: owner\n")
    state["pull"] = judged_pr(judging("low — fine"))
    code, out, _ = check_merge(capsys)
    why = "nothing rates it above low, and `levels.low.merge` is `owner`"
    assert (code, out) == (approvals.WAITING, [OWNER_ONLY + why])
    code = approvals.main(["approve", "merge", "7", "--head", HEAD, "--by", "delegate"])
    assert code == 1
    assert capsys.readouterr().out.splitlines() == [
        f"PR #7: the delegate's go is no go here, since {why}: it waits for the owner's"
    ]
    assert hub == {}


def test_a_high_level_asking_the_owners_own_go_takes_no_delegate_go(
    gating: Gate, capsys: pytest.CaptureFixture[str], hub: dict[str, list[dict[str, Any]]]
) -> None:
    state, _ = gating
    policy("loop:\n  delegate: all\nlevels:\n  high:\n    merge: owner\n")
    state["pull"] = judged_pr(judging("low — fine"), labels=("risk:high",))
    state["pull"]["comments"]["nodes"].append(note(DELEGATE_RECORD))
    hub[HEAD] = [status("owner", DELEGATE_GO)]
    code, out, err = check_merge(capsys)
    why = "`risk:high` is on PR #7, and `levels.high.merge` is `owner`"
    assert (code, out) == (approvals.WAITING, [OWNER_ONLY + why])
    assert "PR #7: risk: high (`risk:high` is on PR #7); merge: owner" in err
    # a low PR under the same policy is the gates' alone
    state["pull"] = judged_pr(judging("low — fine"))
    assert check_merge(capsys)[:2] == (0, [])


def test_the_strictest_action_and_the_highest_level_are_what_the_rating_reports(
    gating: Gate,
) -> None:
    """A PR rated high by its label and critical by a path: the level is critical, and the merge takes the
    strictest action any term asks, with the reason of the first term that asks it."""
    policy(
        "risk:\n  critical_paths:\n    - app.py\nlevels:\n  high:\n    merge: owner\n  critical:\n    merge: go\n"
    )
    loop = Path("docs/agents/loop.md").read_text()
    pr = judged_pr(judging("low — fine"), labels=("risk:high",))
    rated = approvals.rating(pr, [], loop)
    assert rated == approvals.Rated(
        "critical",
        "it touches `app.py`, which `risk.critical_paths` names (`app.py`)",
        "owner",
        "high",
        "`risk:high` is on PR #7, and `levels.high.merge` is `owner`",
    )


def test_a_critical_level_asking_only_a_go_takes_the_delegates(
    gating: Gate, capsys: pytest.CaptureFixture[str], hub: dict[str, list[dict[str, Any]]]
) -> None:
    state, _ = gating
    policy(
        "loop:\n  delegate: all\nrisk:\n  critical_paths:\n    - app.py\nlevels:\n  critical:\n    merge: go\n"
    )
    state["pull"] = judged_pr(judging("low — fine"))
    code, out, err = check_merge(capsys)
    why = "it touches `app.py`, which `risk.critical_paths` names (`app.py`)"
    assert (code, out) == (approvals.WAITING, [DELEGATED_GO_ON_MAIN + f"critical risk: {why}"])
    assert f"PR #7: risk: critical ({why}); merge: go" in err
    state["pull"]["comments"]["nodes"].append(note(DELEGATE_RECORD))
    hub[HEAD] = [status("owner", DELEGATE_GO)]
    code, out, _ = check_merge(capsys)
    assert code == 0
    assert out == [f"PR #7: the go on {HEAD} is the delegate's, under the owner's delegation"]
    # the files it can't read hold for a person, not only the owner
    unread = judged_pr(judging("low — fine"))
    unread["files"]["totalCount"] = 101
    assert approvals.critical(unread, Path("docs/agents/loop.md").read_text()) == (
        "the gate can't read every file the PR changes, so `risk.critical_paths` holds until a person judges them"
    )


# --- critical_paths and its deprecated name ---


@pytest.mark.parametrize("key", ["critical_paths", "merge_paths"])
def test_a_critical_path_refuses_the_delegates_go_under_either_name(
    key: str,
    gating: Gate,
    capsys: pytest.CaptureFixture[str],
    hub: dict[str, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(approvals, "DEPRECATED", set())
    state, _ = gating
    policy(f"loop:\n  delegate: all\nrisk:\n  {key}:\n    - app.py\n")
    state["pull"] = judged_pr(judging("low — fine"))
    code = approvals.main(["approve", "merge", "7", "--head", HEAD, "--by", "delegate"])
    out, err = capsys.readouterr()
    assert code == 1
    assert out.splitlines() == [
        (
            f"PR #7: the delegate's go is no go here, since it touches `app.py`, which `risk.{key}` names (`app.py`),"
            " so only the owner approves its merge: it waits for the owner's"
        )
    ]
    assert hub == {}
    deprecated = [line for line in err.splitlines() if line.startswith("deprecated:")]
    if key == "merge_paths":
        assert deprecated == [
            "deprecated: risk.merge_paths goes in the next release; name its globs `risk.critical_paths`"
        ]
    else:
        assert deprecated == []


def test_both_names_of_the_critical_paths_are_refused_naming_them() -> None:
    loop = loop_md("risk:\n  critical_paths:\n    - a/**\n  merge_paths:\n    - b/**\n")
    with pytest.raises(approvals.Refused) as refused:
        approvals.settings(loop)
    assert str(refused.value) == (
        "loop.md's `risk` sets both `critical_paths` and `merge_paths`, its deprecated name: keep `critical_paths`"
    )


@pytest.mark.parametrize(
    ("levels", "said"),
    [
        ("  medium:\n    merge: go\n", "loop.md has an unknown setting `levels.medium`"),
        (
            "  high:\n    merge: auto\n",
            "loop.md's `levels.high.merge` must be one of gates, go, owner, not 'auto'",
        ),
        (
            "  low:\n    spec: auto\n",
            "loop.md's `levels.low.spec` must be one of policy, owner, not 'auto'",
        ),
        ("  high:\n    approve: go\n", "loop.md has an unknown setting `levels.high.approve`"),
    ],
    ids=["level", "merge", "spec", "key"],
)
def test_an_unknown_level_or_action_is_refused_naming_the_key(levels: str, said: str) -> None:
    with pytest.raises(approvals.Refused) as refused:
        approvals.settings(loop_md(f"levels:\n{levels}"))
    assert str(refused.value) == said


def test_the_defaults_are_the_rule_before_levels() -> None:
    assert approvals.levels(loop_md()) == {
        "low": {"spec": "policy", "merge": "gates", "rerun": False},
        "high": {"spec": "policy", "merge": "go", "rerun": True},
        "critical": {"spec": "policy", "merge": "owner", "rerun": True},
    }
    assert approvals.levels(loop_md("spec:\n  auto: true\n"))["high"] == {
        "spec": "owner",
        "merge": "gates",
        "rerun": True,
    }
    # a level the policy names keeps the defaults of the keys it leaves out
    assert approvals.levels(loop_md("spec:\n  auto: true\nlevels:\n  high:\n    merge: go\n"))[
        "high"
    ] == {
        "spec": "owner",
        "merge": "go",
        "rerun": True,
    }


# --- spec.auto: true and the rating after the spec ---

AUTO_VERIFIER = "spec:\n  auto: true\nrisk:\n  verifier: true\n"


def test_under_spec_auto_a_verifier_rating_waits_for_the_go_unless_its_level_lands_on_the_gates(
    gating: Gate, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rating the spec never saw takes `go` where the level's spec is `owner` and its merge `gates` (the default
    under `spec.auto: true`); `{spec: policy, merge: gates}` lands it on the gates."""
    monkeypatch.setattr(approvals, "writer", lambda _login: True)
    state, _ = gating
    policy(AUTO_VERIFIER)
    state["pull"] = authored(judged_pr(judging("high — it changes the gate")))
    code, out, _ = check_merge(capsys)
    why = (
        f"it matches a risk rule's `verifier` term: the verifier's pass 1 on {HEAD[:7]} says"
        " `Risk: high — it changes the gate`"
    )
    assert (code, out) == (approvals.WAITING, [GO_ON_MAIN + f"high risk: {why}"])
    policy(AUTO_VERIFIER + "levels:\n  high:\n    spec: policy\n    merge: gates\n")
    state["pull"] = authored(judged_pr(judging("high — it changes the gate")))
    code, out, err = check_merge(capsys)
    assert (code, out) == (0, [])
    assert f"PR #7: risk: high ({why}); merge: gates" in err


def test_merge_for_asks_a_go_only_of_a_rating_after_an_owners_spec_on_the_gates() -> None:
    table = {
        "low": {"spec": "policy", "merge": "gates"},
        "high": {"spec": "owner", "merge": "gates"},
        "critical": {"spec": "owner", "merge": "owner"},
    }
    assert approvals.merge_for("high", True, table) == "go"
    assert approvals.merge_for("high", False, table) == "gates"
    assert approvals.merge_for("low", True, table) == "gates"
    assert approvals.merge_for("critical", True, table) == "owner"


def test_a_high_level_on_policy_lets_spec_auto_approve_a_spec_a_rule_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(approvals, "writer", lambda _login: True)
    rule = "spec:\n  auto: true\nrisk:\n  paths:\n    - billing/**\n"
    owned = by("worker", specced(5, "kind:enhancement"))
    pr = pull(reviews=(judging("low — fine"),), files=("billing/pay.py",))
    held = approvals.proofs("build", pr, [owned], loop_md(rule))
    assert "#5 lacks the `approved:spec` label" in held
    loop = loop_md(rule + "levels:\n  high:\n    spec: policy\n")
    assert approvals.proofs("build", pr, [owned], loop) == []
    assert approvals.holds(pr, [owned], loop) == []
    # its merge then takes the level's `merge`, under `spec.auto: true` the gates by default
    assert approvals.rating(pr, [owned], loop).merge == "gates"


@pytest.mark.parametrize("auto", ["true", "\n    - bug"], ids=["auto", "kinds"])
def test_a_critical_level_asking_the_owners_spec_holds_a_spec_the_policy_covers(
    auto: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(approvals, "writer", lambda _login: True)
    loop = loop_md(
        f"spec:\n  auto: {auto}\nrisk:\n  critical_paths:\n    - src/orders/**\nlevels:\n  critical:\n    spec: owner\n"
    )
    bug = by("worker", specced(5, "kind:bug"))
    pr = pull(reviews=(judging("low — fine"),), files=("src/orders/place.py",))
    found = approvals.proofs("build", pr, [bug], loop)
    assert "#5 lacks the `approved:spec` label" in found
    why = "it touches `src/orders/place.py`, which `risk.critical_paths` names (`src/orders/**`), so only the owner"
    lines = approvals.holds(pr, [bug], loop)
    assert len(lines) == 1 and why in lines[0]
    assert (
        approvals.proofs(
            "build", pull(reviews=(judging("low — fine"),), files=("app.py",)), [bug], loop
        )
        == []
    )


@pytest.mark.parametrize("auto", ["true", "\n    - bug"], ids=["auto", "kinds"])
def test_a_low_level_asking_the_owners_spec_holds_every_spec_nothing_raises(
    auto: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(approvals, "writer", lambda _login: True)
    rule = f"spec:\n  auto: {auto}\nrisk:\n  paths:\n    - billing/**\n  critical_paths:\n    - src/orders/**\n"
    loop = loop_md(rule + "levels:\n  low:\n    spec: owner\n  high:\n    spec: policy\n")
    bug = by("worker", specced(5, "kind:bug"))
    low = pull(reviews=(judging("low — fine"),), files=("app.py",))
    assert "#5 lacks the `approved:spec` label" in approvals.proofs("build", low, [bug], loop)
    lines = approvals.holds(low, [bug], loop)
    assert len(lines) == 1
    assert lines[0].endswith("nothing rates it above low, and `levels.low.spec` is `owner`")
    # a spec rated high or critical takes that level's `policy`
    for path in ("billing/pay.py", "src/orders/place.py"):
        raised = pull(reviews=(judging("low — fine"),), files=(path,))
        assert approvals.proofs("build", raised, [bug], loop) == []


def test_without_spec_auto_a_kinds_issue_no_level_holds_needs_no_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`holds` names an issue a kind covers only where its rating takes it from the policy."""
    monkeypatch.setattr(approvals, "writer", lambda _login: True)
    loop = loop_md("spec:\n  auto:\n    - bug\nlevels:\n  low:\n    spec: owner\n")
    pr = pull(reviews=(judging("low — fine"),), files=("app.py",))
    epic = issue(6, "kind:enhancement")
    wayfinder = by("worker", specced(8, "kind:bug", "wayfinder:map"))
    assert approvals.holds(pr, [epic, wayfinder], loop) == []
    assert (
        approvals.holds(
            pr, [by("worker", specced(5, "kind:bug"))], loop_md("spec:\n  auto:\n    - bug\n")
        )
        == []
    )
    assert (
        approvals.holds(
            pr,
            [by("worker", specced(5, "kind:bug"))],
            loop_md("levels:\n  low:\n    spec: owner\n"),
        )
        == []
    )


def test_the_label_is_raised_for_a_rule_match_except_one_the_owners_spec_answered() -> None:
    """risk() is the rating the label shows: under `spec.auto: true` by default a rule's paths held the spec and its
    merge is the gates', so no label; once the level asks a go of every term alike, the label shows it."""
    pr = pull(reviews=(judging("low — fine"),), files=("billing/pay.py",))
    rule = "spec:\n  auto: true\nrisk:\n  paths:\n    - billing/**\n"
    assert approvals.risk(pr, [approved(1)], loop_md(rule)) is None
    assert approvals.rating(pr, [approved(1)], loop_md(rule)) == approvals.Rated(
        "high",
        "it matches a risk rule (path `billing/**`)",
        "gates",
        "high",
        "it matches a risk rule (path `billing/**`)",
    )
    flagged = loop_md(rule + "levels:\n  high:\n    merge: go\n")
    assert (
        approvals.risk(pr, [approved(1)], flagged) == "it matches a risk rule (path `billing/**`)"
    )


# --- land ---


def test_land_prints_the_rating_and_the_action_it_applied(
    lands: dict[str, Any],
    hub: dict[str, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(approvals, "label", lambda *args, **kwargs: None)
    loop = lands["work"] / "docs" / "agents" / "loop.md"
    loop.write_text(
        loop.read_text().replace("risk:\n", "levels:\n  high:\n    merge: gates\nrisk:\n", 1)
    )
    head = lands["open"](7, {".github/ci.yml": "ci\n"})
    lands["pulls"][7]["files"] = {"nodes": [{"path": ".github/ci.yml"}]}
    code = approvals.main(["land", "7"])
    out, err = capsys.readouterr()
    assert code == 0, out
    assert lands["merged"]() == ["7"]
    assert "PR #7: risk: high (it matches a risk rule (path `.github/**`)); merge: gates" in err
    assert head and not any("waits for the owner's go" in line for line in out.splitlines())


def test_a_command_without_the_gates_issues_rates_an_exact_revert_by_what_it_reverts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`approve merge` and `delegate` read the PR's issues themselves where a level other than `critical` asks the
    owner's own go: an exact revert's issues and its original's risk, as `check merge` reads them."""
    named = issue(3, "kind:bug")
    monkeypatch.setattr(approvals, "closing_issues", lambda _pull: [named])
    monkeypatch.setattr(
        approvals, "reverted", lambda n: f"`risk:high` is on #{n}, which it reverts"
    )
    loop = loop_md("levels:\n  high:\n    merge: owner\n")
    monkeypatch.setattr(approvals, "reverts", lambda _pull: (1, []))
    revert = pull(reviews=(judging("low — fine"),), files=("app.py",))
    assert approvals.rated_issues(revert) == []
    assert (revert["named"], revert["reverted"]) == (
        [named],
        "`risk:high` is on #1, which it reverts",
    )
    assert approvals.owners_only(revert, loop) == (
        "`risk:high` is on #1, which it reverts, and `levels.high.merge` is `owner`"
    )
    monkeypatch.setattr(approvals, "reverts", lambda _pull: (None, []))
    plain = pull(reviews=(judging("low — fine"),), files=("app.py",))
    assert approvals.rated_issues(plain) == [named]
    assert "named" not in plain
    assert approvals.owners_only(plain, loop) is None
    assert approvals.owners_only(plain, loop, [issue(3, "risk:high")]) == (
        "`risk:high` is on #3, and `levels.high.merge` is `owner`"
    )


def test_under_spec_auto_a_revert_rates_after_the_spec_and_asks_a_go() -> None:
    """An exact revert of a high-risk change is not read by the spec's hold, so under the default `high` of
    `spec.auto: true` ({owner, gates}) it asks a go."""
    pr = pull(reviews=(judging("low — fine"),), files=("app.py",))
    pr["reverted"] = "`risk:high` is on #1, which it reverts"
    rated = approvals.rating(pr, [], loop_md("spec:\n  auto: true\n"))
    assert (rated.level, rated.merge) == ("high", "go")


def test_a_critical_path_asking_only_a_go_lets_a_delegate_give_it() -> None:
    loop = loop_md("risk:\n  critical_paths:\n    - app.py\nlevels:\n  critical:\n    merge: go\n")
    pr = pull(reviews=(judging("low — fine"),), files=("app.py",))
    assert approvals.owners_only(pr, loop) is None
    assert approvals.rating(pr, [], loop).merge == "go"


def test_into_a_feature_branch_the_wait_names_the_level_that_asks_the_go() -> None:
    pr = pull(reviews=(judging("low — fine"),), files=("app.py",))
    pr["baseRefName"] = "stream"
    assert approvals.owner_waits(pr, [], loop_md("levels:\n  low:\n    merge: go\n")) == [
        (
            f"PR #7 waits for the owner's go on its head (`approve merge 7 --head {HEAD} --by owner`, or"
            " `approved:merge`), since it is low risk: nothing rates it above low"
        )
    ]


def test_the_gate_raises_no_label_that_would_ask_more_than_the_level_it_shows(
    gating: Gate, capsys: pytest.CaptureFixture[str], added: list[tuple[int, str, bool]]
) -> None:
    """`critical: {merge: gates}` beside the default `high` ({policy, go}): the gate adds no `risk:high`, which read back
    as a `high` rating would wait for a go the policy never asked of a critical change."""
    state, _ = gating
    policy(
        "risk:\n  verifier: true\n  critical_paths:\n    - app.py\nlevels:\n  critical:\n    merge: gates\n"
    )
    state["pull"] = judged_pr(judging("low — fine"))
    code, out, err = check_merge(capsys)
    assert (code, out, added) == (0, [], [])
    assert (
        "PR #7: risk: critical (it touches `app.py`, which `risk.critical_paths` names (`app.py`)); merge: gates"
        in err
    )
    # where `high` asks no more than the gates, the label shows the rating
    policy(
        "risk:\n  verifier: true\n  critical_paths:\n    - app.py\nlevels:\n  high:\n    merge: gates\n"
        "  critical:\n    merge: gates\n"
    )
    state["pull"] = judged_pr(judging("low — fine"))
    assert check_merge(capsys)[0] == 0
    assert (7, "risk:high", True) in added
