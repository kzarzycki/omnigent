#!/usr/bin/env bash
# The gate's selftest and sync-fork.sh refusing a checkout that is not the fork.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
python3 "$here/gate.py" --selftest
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
# A checkout with a commit and a dirty file that lacks one of the fork's pieces: the guard must
# exit 2 naming it, before stashing, fetching or switching branch.
refuses() {
  missing="$1"; shift
  repo="$tmp/$missing"
  git init -q -b work "$repo"
  git -C "$repo" -c user.name=t -c user.email=t@t -c commit.gpgsign=false commit -q --allow-empty -m init
  for setup in "$@"; do git -C "$repo" $setup; done
  echo wip > "$repo/file"
  before="$(git -C "$repo" status --porcelain; git -C "$repo" stash list; git -C "$repo" branch --show-current)"
  code=0; out="$(cd "$repo" && bash "$here/sync-fork.sh" 2>&1)" || code=$?
  after="$(git -C "$repo" status --porcelain; git -C "$repo" stash list; git -C "$repo" branch --show-current)"
  [ "$code" -eq 2 ] || { echo "missing $missing: sync-fork.sh exited $code, want 2: $out" >&2; exit 1; }
  case "$out" in *"'$missing'"*) ;; *) echo "missing $missing: refusal does not name it: $out" >&2; exit 1 ;; esac
  [ "$before" = "$after" ] || { echo "missing $missing: sync-fork.sh changed the checkout" >&2; exit 1; }
  echo "sync-fork.sh refuses a checkout without $missing (exit 2, unchanged)"
}
refuses upstream "remote add origin $tmp/none" "branch mine"
refuses origin "remote add upstream $tmp/none" "branch mine"
refuses mine "remote add upstream $tmp/none" "remote add origin $tmp/none"
