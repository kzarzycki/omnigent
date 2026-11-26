"""gatekeeper.py: an agent-made pack bump gets the `bot.<name>` fast path by what it is, not by who committed it.

A cloud agent commits as `Claude <noreply@anthropic.com>`, and GitHub ties that address to an unrelated person's
account (`claude`), so the commits read as another account's push, not as no account, and the commit-author check
refused the exemption. Under a `branch` section a bump whose files are the released tag's byte for byte
(pin_target) needs no author check: only a writer pushes that branch, and the content is the release's."""

from __future__ import annotations

import re
from typing import Any

import pytest
from approvals_harness import approvals, pull
from test_approvals import LOOP, add, authored
from test_approvals_pin_bump import BLOB, OLD, VENDORED, bump, change

# the consumer's section: its sync writes `.claude/skills/` copies too, so its paths name them
PACK_BUMP = add(
    LOOP,
    "bot",
    "pack-bump:\n  branch: claude/ready-pack-bump-\n  paths:\n    - apm.yml\n    - apm.lock.yaml\n"
    '    - ".agents/**"\n    - ".claude/**"\n',
)
BRANCH = "claude/ready-pack-bump-engineering-v0.99.0"
# GitHub's REST and GraphQL name `Claude <noreply@anthropic.com>` as the user account `claude`, for author and committer
AGENT = ("claude", "claude")


def agent_bump(
    files: tuple[str, ...],
    *,
    commits: tuple[tuple[str | None, str | None], ...] = (AGENT,),
    branch: str = BRANCH,
    moved: tuple[str, ...] = (),
) -> dict[str, Any]:
    """The PR node of a pack bump a cloud agent pushed under the writer's token: `files` changed (`moved` among them
    renamed), each commit's author and committer account from `commits`."""
    pr = {
        **pull(files=files),
        "author": {"__typename": "User", "login": "writer"},
        "headRefName": branch,
    }
    for node in pr["files"]["nodes"]:
        if node["path"] in moved:
            node["changeType"] = "RENAMED"
    pr["authored"] = {"nodes": [authored(*by) for by in commits]}
    return pr


def test_the_agents_identity_is_another_account_so_only_the_content_proof_exempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cause: the agent's commits name the `claude` account, not the PR's author nor no account. Before the fix
    this refused; now the released tag's files prove the bump."""
    files = bump(monkeypatch)
    assert approvals.exempt(agent_bump(files), PACK_BUMP)
    # the same bump by an account-less identity, as before, and by the PR's author
    assert approvals.exempt(agent_bump(files, commits=((None, None),)), PACK_BUMP)
    assert approvals.exempt(agent_bump(files, commits=(("writer", None),)), PACK_BUMP)


def test_without_the_content_proof_another_accounts_commit_still_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A vendored file that isn't the tag's, or a file no tag ships, leaves only the author check, which another
    account's commit fails."""
    wrong = bump(monkeypatch, blob="9" * 40)
    assert not approvals.exempt(agent_bump(wrong), PACK_BUMP)
    assert approvals.exempt(agent_bump(wrong, commits=((None, None),)), PACK_BUMP)
    hook = bump(monkeypatch, extra=(change(".agents/hooks/pre.sh"),))
    assert not approvals.exempt(agent_bump(hook), PACK_BUMP)


def test_the_content_proof_keeps_every_other_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    files = bump(monkeypatch)
    forked = agent_bump(files)
    forked["isCrossRepository"] = True  # a fork may name any branch
    assert not approvals.exempt(forked, PACK_BUMP)
    assert not approvals.exempt(agent_bump(files, branch="claude/ready-feature"), PACK_BUMP)
    labelled = agent_bump(files)
    labelled["labels"]["nodes"].append({"name": "verify"})
    assert not approvals.exempt(labelled, PACK_BUMP)
    # a login section names a bot by its account, so the proof never stands in for its commits
    login = add(
        LOOP,
        "bot",
        'pin-bot[bot]:\n  paths:\n    - apm.yml\n    - apm.lock.yaml\n    - ".claude/**"\n',
    )
    bot = {**agent_bump(files), "author": {"__typename": "Bot", "login": "pin-bot"}}
    assert not approvals.exempt(bot, login)
    assert approvals.exempt({**bot, "authored": {"nodes": [authored("pin-bot")]}}, login)
    # the section's paths still bound it: one that names no `.claude/` copy refuses them
    narrow = re.sub(r'\n *- "\.claude/\*\*"', "", PACK_BUMP)
    assert not approvals.exempt(agent_bump(files), narrow)


def test_a_rename_the_tag_makes_is_proven_and_one_unproven_still_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A release that moves a skill file shows as a rename; the tag proves it, so it no longer refuses the bump."""
    files = bump(monkeypatch, extra=(change(VENDORED, BLOB, "renamed", previous_filename=OLD),))
    shown = tuple(
        dict.fromkeys(path for path in files if path != OLD)
    )  # GraphQL gives the new path only
    assert approvals.exempt(agent_bump(shown, moved=(VENDORED,)), PACK_BUMP)
    # unproven, a rename refuses whoever committed it
    monkeypatch.setattr(approvals, "pin_target", lambda _pull: None)
    assert not approvals.exempt(
        agent_bump(shown, moved=(VENDORED,), commits=((None, None),)), PACK_BUMP
    )
