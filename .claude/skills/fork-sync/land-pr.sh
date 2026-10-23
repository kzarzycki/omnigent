#!/usr/bin/env bash
# Land a pull request of the omnigent fork into `mine` as one signed commit.
#
# Never merge a fork PR on GitHub: GitHub's squash or merge commit carries
# GitHub's signature, not one from ~/.config/omnigent/allowed_signers, so every
# machine's omnigent-update refuses that head and sync-fork.sh refuses to take
# it. This squashes the PR here instead, where commits are signed, pushes the
# signed commit to the PR's branch and then fast-forwards `mine` to it, so
# GitHub shows the PR as merged. A PR from another repository is refused: its
# branch isn't ours to push, so GitHub could only show it closed.
#
# The PR must contain origin/mine (rebase it first), so the squash can't undo a
# commit landed since it branched.
#
# Usage, from the fork checkout of a landing host (AGENTS.md, Landing PRs into
# `mine`): bash .claude/skills/fork-sync/land-pr.sh <pr>
set -euo pipefail

FORK=origin
PATCHED=mine
# the trust list omnigent-update checks, so a squash the machines would refuse never lands
SIGNERS="${OMNIGENT_ALLOWED_SIGNERS:-$HOME/.config/omnigent/allowed_signers}"
pr=${1:?usage: land-pr.sh <pr>}

die() { echo "!! $*" >&2; exit 1; }

git remote get-url "$FORK" >/dev/null 2>&1 \
  || { echo "!! no '$FORK' remote in $PWD — run this from the omnigent fork checkout" >&2; exit 2; }
[ "$(git config gpg.format)" = ssh ] && [ -n "$(git config user.signingkey)" ] \
  || { echo "!! this checkout doesn't sign with an SSH key: see AGENTS.md, Landing PRs into mine" >&2; exit 2; }

# gh's own default repo can be upstream; the PR must be the one origin's refs name
repo=$(git remote get-url "$FORK" | sed -E 's#\.git$##; s#.*[:/]([^/]+/[^/]+)$#\1#')
IFS=$'\t' read -r state base branch cross title < <(
  gh pr view "$pr" -R "$repo" --json state,baseRefName,headRefName,isCrossRepository,title \
    --jq '[.state, .baseRefName, .headRefName, .isCrossRepository, .title] | @tsv')
[ "$state" = OPEN ] || die "PR #$pr is $state"
[ "$base" = "$PATCHED" ] || die "PR #$pr targets $base, not $PATCHED"
[ "$cross" = false ] || die "PR #$pr comes from another repository: push its branch to $repo first"

git fetch -q "$FORK" "$PATCHED"
git fetch -q "$FORK" "pull/$pr/head"  # alone: FETCH_HEAD names the first ref fetched
head=$(git rev-parse FETCH_HEAD)
git merge-base --is-ancestor "$FORK/$PATCHED" "$head" \
  || die "PR #$pr does not contain $FORK/$PATCHED: rebase it onto $PATCHED first"

tree=$(git rev-parse "$head^{tree}")
commit=$(git commit-tree -S -p "$FORK/$PATCHED" -m "$title (#$pr)" "$tree")
git -c gpg.ssh.allowedSignersFile="$SIGNERS" verify-commit "$commit" 2>/dev/null \
  || die "the squash ${commit:0:9} is not signed by a key in $SIGNERS"

# The PR's own branch first: GitHub marks a PR merged once its head lands in the base.
git push -q --force-with-lease="refs/heads/$branch:$head" "$FORK" "$commit:refs/heads/$branch"
git push -q "$FORK" "$commit:refs/heads/$PATCHED"
echo "==> PR #$pr landed in $PATCHED as ${commit:0:9}"
