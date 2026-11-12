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
# pin-pr-target.sh: bare `gh` resolves to the fork, also over a default left on upstream,
# worktrees share the pin, and a clone without origin is left alone.
fork="kzarzycki/omnigent"
root="$(cd "$here/../../.." && pwd)"
pinned="$tmp/pinned"
git init -q -b work "$pinned"
git -C "$pinned" -c user.name=t -c user.email=t@t -c commit.gpgsign=false commit -q --allow-empty -m init
git -C "$pinned" remote add origin "https://github.com/$fork"
git -C "$pinned" remote add upstream https://github.com/omnigent-ai/omnigent
git -C "$pinned" config remote.upstream.gh-resolved base
(cd "$pinned" && bash "$here/pin-pr-target.sh")
[ "$(git -C "$pinned" config --get remote.origin.gh-resolved)" = "$fork" ] \
  || { echo "pin-pr-target.sh did not pin origin to $fork" >&2; exit 1; }
! git -C "$pinned" config --get remote.upstream.gh-resolved >/dev/null \
  || { echo "pin-pr-target.sh left the default on upstream" >&2; exit 1; }
if command -v gh >/dev/null; then
  seen="$(cd "$pinned" && gh repo set-default --view 2>&1)"
  [ "$seen" = "$fork" ] || { echo "gh resolves to '$seen' after the pin, want $fork" >&2; exit 1; }
fi
git -C "$pinned" worktree add -q --detach "$tmp/pinned-worktree"
[ "$(git -C "$tmp/pinned-worktree" config --get remote.origin.gh-resolved)" = "$fork" ] \
  || { echo "a worktree does not read the pin" >&2; exit 1; }
git init -q -b work "$tmp/no-origin"
git -C "$tmp/no-origin" remote add upstream https://github.com/omnigent-ai/omnigent
git -C "$tmp/no-origin" config remote.upstream.gh-resolved base
before="$(git -C "$tmp/no-origin" config --list --local)"
(cd "$tmp/no-origin" && bash "$here/pin-pr-target.sh") \
  || { echo "pin-pr-target.sh fails in a clone without origin" >&2; exit 1; }
[ "$(git -C "$tmp/no-origin" config --list --local)" = "$before" ] \
  || { echo "pin-pr-target.sh changed the config of a clone without origin" >&2; exit 1; }
# The task runs the script, the gate runs the task first, and AGENTS.md names the explicit command.
python3 - "$root/mise.toml" <<'PY' || { echo "mise.toml: pin-pr-target is not wired into check:all" >&2; exit 1; }
import sys, tomllib
tasks = tomllib.load(open(sys.argv[1], "rb"))["tasks"]
assert "pin-pr-target.sh" in tasks["pin-pr-target"]["run"]
assert "pin-pr-target" in tasks["check:all"]["depends"]
PY
grep -qF "gh pr create --repo $fork --base mine" "$root/AGENTS.md" \
  || { echo "AGENTS.md does not name the explicit PR command" >&2; exit 1; }
echo "pin-pr-target.sh: ok"
