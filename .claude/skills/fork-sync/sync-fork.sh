#!/usr/bin/env bash
# Sync this fork with upstream through a merge PR; `mine` is never rewritten.
#
#   main  = pristine mirror of upstream/main (fast-forward only)
#   mine  = main + the fork's patches; moves only by PRs landed on it
#
# The audited TARGET is merged (--no-ff) into a branch sync/<first 12 chars of
# TARGET> started from origin/mine, the branch is pushed and a PR against mine
# is opened through `gh api` (REST: GraphQL fails behind the cloud proxy). The PR
# lands as a merge commit, never a squash, so upstream's history stays in mine and
# `git diff main...mine` lists only what the fork carries on top of upstream.
# Deploying the result is not done here: each machine runs omnigent-update
# (dotagents) from an Omnigent scheduled task.
#
# A dirty working tree is auto-stashed before the sync and restored after, so
# in-progress edits survive. Runs against the git checkout of the current
# directory, and refuses to touch it unless it has the remotes and the branch
# below: started anywhere else, it exits 2 before changing anything.
#
# TARGET defaults to the newest upstream mainline commit at least 3 days old: a
# compromised upstream change usually surfaces within days, so a short lag keeps
# most of them off every machine. The sync skill passes the exact commit it audited.
#
# An open sync/* PR against the base stops the run (exit 1,
# `VERDICT: HOLD sync PR #N not landed yet`), so two syncs never stack. A merge
# conflict stops it too, mid-merge: resolve, `git commit`, and rerun: the rerun
# sees the finished merge on the sync branch and goes on to push and open the PR.
#
# Usage, from the fork checkout:
#   bash <this directory>/sync-fork.sh [TARGET [PR_BODY_FILE]]
# PR_BODY_FILE holds the audit summary and the VERDICT line for the PR body.
# Environment: SYNC_BASE (branch the PR targets, default mine; a scratch branch
# for a dry run) and SYNC_REPO (owner/name for gh api, default read from origin).
set -euo pipefail

UPSTREAM=upstream      # remote -> omnigent-ai/omnigent
FORK=origin            # remote -> your fork
MIRROR=main            # pristine upstream mirror branch
PATCHED=${SYNC_BASE:-mine}   # branch the sync PR targets

cd "$(git rev-parse --show-toplevel)"

for remote in "$UPSTREAM" "$FORK"; do
  git remote get-url "$remote" >/dev/null 2>&1 \
    || { echo "!! no '$remote' remote in $PWD — run this from the omnigent fork checkout" >&2; exit 2; }
done
{ git rev-parse -q --verify "refs/heads/$PATCHED" || git rev-parse -q --verify "refs/remotes/$FORK/$PATCHED"; } >/dev/null \
  || { echo "!! no '$PATCHED' branch in $PWD — run this from the omnigent fork checkout" >&2; exit 2; }
[ ! -f "$(git rev-parse --git-path MERGE_HEAD)" ] \
  || { echo "!! a merge is in progress: resolve it, 'git commit', then rerun" >&2; exit 1; }

start_branch=$(git rev-parse --abbrev-ref HEAD)

stashed=0
if [ -n "$(git status --porcelain)" ]; then
  echo "==> stashing dirty working tree"
  git stash push -u -m "sync-fork autostash" >/dev/null
  stashed=1
fi

restore() {
  if [ -f "$(git rev-parse --git-path MERGE_HEAD)" ]; then
    [ "$stashed" = 0 ] || echo "!! merge stopped: your WIP is in 'git stash list' (sync-fork autostash); pop it after the merge is committed" >&2
    return
  fi
  git checkout -q "$start_branch" 2>/dev/null || true
  if [ "$stashed" = 1 ]; then
    echo "==> restoring stashed working tree"
    git stash pop || echo "!! stash pop conflicted — your WIP is safe in 'git stash list'; resolve manually" >&2
  fi
}
trap restore EXIT

# The mirror push is non-fatal: the next run pushes it.
push() {
  git push "$@" || echo "!! push failed ($*) — fork remote not updated" >&2
}

echo "==> fetching $UPSTREAM and $FORK"
git fetch "$UPSTREAM"
git fetch "$FORK" "$PATCHED"

TARGET=$(git rev-parse --verify "${1:-$(git rev-list -1 --first-parent --before='3 days ago' "$UPSTREAM/$MIRROR")}^{commit}")
BODY_FILE=${2:-}
SYNC_BRANCH="sync/${TARGET:0:12}"
REPO=${SYNC_REPO:-$(git remote get-url "$FORK" | sed -E 's#^.*github\.com[:/]##; s#\.git$##')}

echo "==> fast-forwarding $MIRROR to $UPSTREAM/$MIRROR"
git branch -f "$MIRROR" "$UPSTREAM/$MIRROR"
push "$FORK" "$MIRROR"

open_sync=$(gh api "repos/$REPO/pulls?state=open&base=$PATCHED&per_page=100" \
  --jq '.[] | select(.head.ref | startswith("sync/")) | .number' | head -n1)
if [ -n "$open_sync" ]; then
  echo "VERDICT: HOLD sync PR #$open_sync not landed yet"
  exit 1
fi

if git merge-base --is-ancestor "$TARGET" "$FORK/$PATCHED"; then
  echo "==> $PATCHED already contains ${TARGET:0:9}; nothing to merge"
  exit 0
fi

# A rerun after a hand-resolved conflict finds the finished merge on the branch.
if git merge-base --is-ancestor "$TARGET" "refs/heads/$SYNC_BRANCH" 2>/dev/null; then
  echo "==> $SYNC_BRANCH already has ${TARGET:0:9} merged"
  git checkout -q "$SYNC_BRANCH"
else
  git checkout -q -B "$SYNC_BRANCH" "$FORK/$PATCHED"
  echo "==> merging ${TARGET:0:9} into $SYNC_BRANCH (from $FORK/$PATCHED)"
  git merge --no-ff -m "Merge upstream ${TARGET:0:12} into $PATCHED" "$TARGET" \
    || { echo "!! merge conflicts: resolve them, 'git commit', then rerun this script" >&2; exit 1; }
fi

echo "==> pushing $SYNC_BRANCH"
git push "$FORK" "$SYNC_BRANCH"

body=$(mktemp)
if [ -n "$BODY_FILE" ]; then
  cat "$BODY_FILE" > "$body"
else
  { echo "Audit summary not supplied."; echo; git log --oneline "$FORK/$PATCHED..$TARGET" | head -n 50; } > "$body"
fi
echo "==> opening a PR against $PATCHED"
gh api "repos/$REPO/pulls" -f title="Sync upstream ${TARGET:0:12} into $PATCHED" \
  -f head="$SYNC_BRANCH" -f base="$PATCHED" -F body=@"$body" --jq .html_url
rm -f "$body"

echo "==> done. $SYNC_BRANCH pushed; land the PR as a merge commit, never a squash."
