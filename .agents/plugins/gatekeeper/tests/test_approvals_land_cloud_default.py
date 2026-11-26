"""approvals.py: a cloud land is the default, and needs no `land` section on a merge-queue base (#1356)."""

from __future__ import annotations

import pytest
from approvals_harness import approvals

NO_LAND = "# Loop facts\n\n## Settings\n\n```yaml\npractice:\n  spec: to-spec\n```\n"


def test_a_cloud_land_on_a_merge_queue_base_needs_no_land_section(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`where` unset is `both` and GitHub's queue merges and signs, so a cloud session needs no section nor key."""
    monkeypatch.setenv(approvals.CLOUD, "true")
    with approvals.placed(NO_LAND, True):
        assert approvals.SIGNING == []
    # no queue, no `signed_squash_to`: GitHub's REST merge, which signs, so still no key (#1712)
    with approvals.placed(NO_LAND, False):
        assert approvals.SIGNING == []


@pytest.mark.parametrize(
    "land", ["merge_commit_heads: [sync/*]", "where: both", "signing_key_env: KEY"]
)
def test_a_land_section_without_signed_squash_to_lands_over_rest_on_a_base_without_the_queue(
    monkeypatch: pytest.MonkeyPatch, land: str
) -> None:
    """No signing is set up, so GitHub merges over REST: no refusal, no key check, never a KeyError (#1712)."""
    monkeypatch.setenv(approvals.CLOUD, "true")
    loop = NO_LAND.replace("practice:", f"land:\n  {land}\npractice:")
    with approvals.placed(loop, False):
        assert approvals.SIGNING == []
    with approvals.placed(loop, True):
        pass


def test_land_where_host_still_refuses_a_cloud_session_on_a_merge_queue_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(approvals.CLOUD, "true")
    loop = NO_LAND.replace("practice:", "land:\n  where: host\npractice:")
    with (
        pytest.raises(
            approvals.Refused,
            match="`land.where` is `host`, so land does not run in a cloud session",
        ),
        approvals.placed(loop, True),
    ):
        pass


def test_where_host_names_where_even_on_a_base_without_the_queue_or_signed_squash_to(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adding a queue or `signed_squash_to` would not let it land, so the `where` line is the one it gets."""
    monkeypatch.setenv(approvals.CLOUD, "true")
    loop = NO_LAND.replace("practice:", "land:\n  where: host\npractice:")
    with (
        pytest.raises(approvals.Refused, match="`land.where` is `host`"),
        approvals.placed(loop, False),
    ):
        pass
