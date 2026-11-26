"""Every cloud session logs its own gh calls with gh-quota's shim and posts the tally, since its log stays in the VM
where no host reads it; a host session's brief carries no such line."""

import re
from pathlib import Path

from test_omnigent_agent import omnigent_agent as oa

REPO = Path(__file__).resolve().parents[2]


def test_a_cloud_brief_installs_the_shim_and_posts_its_counts() -> None:
    text = oa.first_message(True, False, 9, "/tmp/r.md", None, "Fix the bug.")
    found = re.search(r'"\$HOME/pack/(github/skills/gh-quota/scripts/gh_quota\.py)" install', text)
    assert found, text
    assert (REPO / found[1]).is_file()  # the path the brief names is the pack's
    assert "gh_quota.py counts --minutes" in text and "`curl` calls" in text
    assert "`gh-quota: not installed`" in text  # an empty tally is never read as no calls
    assert text.index(oa.CLOUD_GH_QUOTA.strip()) < text.index("The pack's scripts")


def test_a_host_brief_names_no_shim() -> None:
    assert "gh_quota.py" not in oa.first_message(False, False, 9, "/tmp/r.md", None, "Fix the bug.")
