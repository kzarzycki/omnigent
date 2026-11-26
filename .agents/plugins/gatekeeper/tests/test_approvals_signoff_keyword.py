"""approvals.py: a PR linking a sign-off issue with the wrong keyword is refused out loud, by `land` and already at
`check build`, in a line naming the keyword the PR says, the one it needs and what requires it (#1001)."""

from __future__ import annotations

import shlex
import sys
from typing import Any

import pytest
from approvals_harness import (
    GITHUB_RULE,
    LOOP,
    TRACKER,
    add,
    approvals,
    approved,
    git_in,
    held,
    labelled,
    lands,  # noqa: F401
    pull,
)

RULE = "labels:\n  - area:web\npaths:\n  - app/**\n"
SIGNED = add(LOOP, "signoff", RULE)

# the line for a PR saying `Closes #105` of an issue the label `area:web` puts under the sign-off rule
CLOSED = (
    "#105: the PR says `Closes #105`, but #105 waits for the owner's sign-off after landing, since it matches a"
    " sign-off rule (label `area:web`): use `Implements #105`, which leaves it open"
)


def linking(
    body: str, *labels: str, files: tuple[str, ...] = ()
) -> tuple[dict[str, Any], dict[str, Any]]:
    """A PR whose body is `body` plus an Evidence section, and its issue #1 with `labels`."""
    return pull(body=f"{body}\n\n## Evidence\n\nlog line\n", files=files), approved(1, *labels)


def test_land_of_a_pr_closing_a_signoff_issue_prints_the_line_and_exits_1(
    lands: dict[str, Any],
) -> None:
    (lands["work"] / "docs" / "agents" / "loop.md").write_text(add(GITHUB_RULE, "signoff", RULE))
    lands["open"](5, {"5.txt": "5\n"}, body="Closes #105\n\n## Evidence\n\nlog line\n")
    code, lines = lands["land"]("5")
    assert (code, lines) == (1, [CLOSED])
    assert lands["merged"]() == []


@pytest.mark.parametrize(
    ("body", "labels", "files", "line"),
    [
        (
            "Fixes #1",
            ("area:web",),
            (),
            (
                "#1: the PR says `Fixes #1`, but #1 waits for the owner's sign-off after landing, since it matches a"
                " sign-off rule (label `area:web`): use `Implements #1`, which leaves it open"
            ),
        ),
        (
            # the keyword linking #1 itself, not another issue's nor a weaker link to #1
            "Closes #2\nPart of #1\nFixes #1",
            ("area:web",),
            (),
            (
                "#1: the PR says `Fixes #1`, but #1 waits for the owner's sign-off after landing, since it matches"
                " a sign-off rule (label `area:web`): use `Implements #1`, which leaves it open"
            ),
        ),
        (
            "closes: #1",
            ("area:api",),
            ("app/main.swift",),
            (
                "#1: the PR says `Closes #1`, but #1 waits for the owner's sign-off after landing, since it matches a"
                " sign-off rule (path `app/**`): use `Implements #1`, which leaves it open"
            ),
        ),
        (
            "Implements #1",
            ("area:api",),
            ("api/a.py",),
            (
                "#1: the PR says `Implements #1`, but #1 needs no sign-off (no sign-off rule matches): use `Closes #1`,"
                " since `Implements` would leave it open for a sign-off nobody asked for"
            ),
        ),
    ],
)
def test_the_line_names_the_prs_keyword_the_needed_one_and_what_requires_it(
    body: str, labels: tuple[str, ...], files: tuple[str, ...], line: str
) -> None:
    pr, issue = linking(body, *labels, files=files)
    assert approvals.linked(pr, [issue], SIGNED) == [line]


def test_a_link_github_made_with_no_body_line_is_said_as_such() -> None:
    pr, issue = linking("the change", "area:web")
    assert approvals.linked(pr, [issue], SIGNED) == [
        (
            "#1: GitHub links #1 to the PR as closing, with no line in its body, but #1 waits for the owner's sign-off"
            " after landing, since it matches a sign-off rule (label `area:web`): use `Implements #1`, which leaves it"
            " open"
        )
    ]


@pytest.mark.parametrize("signoff", [True, False])
def test_check_build_on_a_pr_reports_the_same_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], signoff: bool
) -> None:
    """Where the lead hands the PR over, before it reaches the landing queue; without a `signoff` rule no keyword is
    checked, as before."""
    monkeypatch.setattr(approvals, "project", lambda: (TRACKER, SIGNED if signoff else LOOP))
    spec = held(approved(105, "kind:bug", "area:web"), events=(labelled("loop"),))
    pr = pull(body="Closes #105\n")
    pr["closingIssuesReferences"]["nodes"] = [spec]
    monkeypatch.setattr(approvals, "pull_request", lambda _pr: pr)
    code = approvals.main(["check", "build", "7"])
    out = capsys.readouterr().out.splitlines()
    if signoff:
        assert (code, out) == (1, [CLOSED])
    else:
        assert CLOSED not in out


CHECK = """import sys
for n in range(40):
    print(f"line {n}")
print("why: the head forked before the restructuring")
sys.exit(1)
"""


def test_a_pre_land_refusal_prints_the_checks_last_lines_on_every_land(
    lands: dict[str, Any],
) -> None:
    """Its comment goes on the PR once per head, so a land again on that head said only the exit code."""
    work = lands["work"]
    git_in(work, "switch", "-q", "--detach", "origin/main")
    (work / "check.py").write_text(CHECK)
    git_in(work, "add", "check.py")
    git_in(work, "commit", "-qm", "the pre-land check")
    git_in(work, "push", "-q", "origin", "HEAD:main")
    git_in(work, "fetch", "-q", "origin")
    command = shlex.join([sys.executable, "check.py"])
    (work / "docs" / "agents" / "loop.md").write_text(
        add(GITHUB_RULE, "loop", f"pre_land: {command}\n")
    )
    lands["open"](8, {"8.txt": "8\n"})
    for _ in range(2):
        code, lines = lands["land"]("8")
        assert code == 1, lines
        at = next(
            i for i, line in enumerate(lines) if line.startswith(f"PR #8: `{command}` refused it")
        )
        tail = [f"  line {n}" for n in range(40 - approvals.LOG_TAIL + 1, 40)]
        assert lines[at + 1 : at + 1 + approvals.LOG_TAIL] == [
            *tail,
            "  why: the head forked before the restructuring",
        ]
    assert len([body for _, body in lands["comments"] if body.startswith(approvals.PRE_LAND)]) == 1
