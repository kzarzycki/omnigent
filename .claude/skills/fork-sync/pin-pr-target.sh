#!/bin/bash
# Make bare `gh` commands in this clone and its worktrees resolve to the fork,
# so a bare `gh pr create` cannot open a PR on upstream.
set -euo pipefail
fork="kzarzycki/omnigent"
# A clone without origin has no fork remote to pin: leave its settings alone.
git remote get-url origin >/dev/null 2>&1 || exit 0
# gh takes the first remote with a default set, so one left on another remote would win.
for remote in $(git remote); do
  git config --unset-all "remote.$remote.gh-resolved" 2>/dev/null || true
done
git config remote.origin.gh-resolved "$fork"
