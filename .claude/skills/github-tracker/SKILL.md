---
name: github-tracker
description: The GitHub tracker and forge conventions the engineering loop and the gatekeeper run on - issue, label, assignee, review-trail and board operations with gh, label sync (labels.py) and board moves (board.py). Use when a project's docs/agents/issue-tracker.md names GitHub as its tracker, or when syncing a repo's labels or moving an issue on its board.
metadata:
  requires: "gh uv"
---

# GitHub tracker

[github.md](github.md) is the GitHub tracker's realization file: how the loop's issue
conventions are done with `gh`. A project whose `docs/agents/issue-tracker.md` says
`Tracker: GitHub (github-tracker's github.md)`, or `engineering-loop's github.md` as before
this package split, follows it in place.

Three scripts, each run with `uv run <this skill's folder>/scripts/<name>.py` from the
checkout, read the label names and the tracker settings through the `gatekeeper` skill's
`gatekeeper.py`, found where the host installed that skill (the package depends on
`gatekeeper`), and exit naming that skill when it is missing:

- `scripts/labels.py` syncs the repo's labels with the loop's fixed set and the project's
  components and categories (github.md, Labels).
- `scripts/board.py` moves an issue to a board column, or prints the loop's column for it
  (github.md, Board).
- `scripts/rulesets.py` commits what the repo's rulesets are meant to block (`snapshot`) and notices
  when they stop (`check`).

Each docstring is its manual.
