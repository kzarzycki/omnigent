#!/usr/bin/env bash
# The gate's selftest, sync-fork.sh refusing a checkout that is not the fork and its merge-PR flow.
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
# setup.sh needs no signer list or key and leaves commit signing off: GitHub is the trust
# anchor. The sync scenario below runs on unsigned commits throughout.
signing_free="$tmp/signing-free"
git init -q -b main "$signing_free"
git -C "$signing_free" -c user.name=t -c user.email=t@t -c commit.gpgsign=false commit -q --allow-empty -m init
git init -q --bare -b main "$tmp/signing-free-origin.git"
git -C "$signing_free" remote add origin "$tmp/signing-free-origin.git"
git -C "$signing_free" branch -q mine
git -C "$signing_free" push -q origin mine
git -C "$signing_free" config commit.gpgsign true
(cd "$signing_free" && bash "$here/setup.sh" >/dev/null 2>&1) \
  || { echo "setup.sh fails without a signer list or key" >&2; exit 1; }
[ "$(git -C "$signing_free" config --get commit.gpgsign)" = false ] \
  || { echo "setup.sh left commit signing on" >&2; exit 1; }
echo "setup.sh: no signing, no secrets"
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
# sync-fork.sh against a scratch upstream and fork, with `gh api` stubbed and every `git`
# call logged. The fork rejects non-fast-forward pushes; the log catches any forced push.
commit_as_tester() { git -c user.name=t -c user.email=t@t -c commit.gpgsign=false "$@"; }
scratch="$tmp/sync"
mkdir -p "$scratch/bin"
commit_as_tester init -q -b main "$scratch/seed"
printf 'a\n' > "$scratch/seed/a.txt"; printf 'one\ntwo\nthree\n' > "$scratch/seed/c.txt"; printf 'x\n' > "$scratch/seed/d.txt"
commit_as_tester -C "$scratch/seed" add -A; commit_as_tester -C "$scratch/seed" commit -q -m base
commit_as_tester clone -q --bare "$scratch/seed" "$scratch/upstream.git"
commit_as_tester clone -q --bare "$scratch/seed" "$scratch/fork.git"
git -C "$scratch/fork.git" config receive.denyNonFastForwards true
commit_as_tester clone -q "$scratch/fork.git" "$scratch/work"
git -C "$scratch/work" remote add upstream "$scratch/upstream.git"
git -C "$scratch/work" checkout -q -b mine
# The fork's own commits: two that get promoted upstream as one squash, one that upstream
# tweaks, and one that stays fork-only.
printf 'p1\n' > "$scratch/work/promoted.txt"; commit_as_tester -C "$scratch/work" add -A; commit_as_tester -C "$scratch/work" commit -q -m promoted-1
printf 'p1\np2\n' > "$scratch/work/promoted.txt"; commit_as_tester -C "$scratch/work" add -A; commit_as_tester -C "$scratch/work" commit -q -m promoted-2
printf 'one\nfork two\nthree\n' > "$scratch/work/c.txt"; commit_as_tester -C "$scratch/work" commit -q -am tweaked
printf 'only\n' > "$scratch/work/fork-only.txt"; commit_as_tester -C "$scratch/work" add -A; commit_as_tester -C "$scratch/work" commit -q -m fork-only
git -C "$scratch/work" push -q origin mine
git -C "$scratch/work" checkout -q -b work
# Upstream: the promotion (one squash), then its own tweak of the same line as the fork's.
commit_as_tester clone -q "$scratch/upstream.git" "$scratch/upstream-work"
printf 'p1\np2\n' > "$scratch/upstream-work/promoted.txt"; commit_as_tester -C "$scratch/upstream-work" add -A; commit_as_tester -C "$scratch/upstream-work" commit -q -m "promote fork change"
first_target="$(git -C "$scratch/upstream-work" rev-parse HEAD)"
printf 'one\nupstream two\nthree\n' > "$scratch/upstream-work/c.txt"; commit_as_tester -C "$scratch/upstream-work" commit -q -am "upstream tweak"
conflict_target="$(git -C "$scratch/upstream-work" rev-parse HEAD)"
git -C "$scratch/upstream-work" push -q origin main
cat > "$scratch/bin/gh" <<'STUB'
#!/usr/bin/env bash
# Stub for `gh api`: logs each call; the open-PR listing answers from $STUB_OPEN_SYNC.
echo "$*" >> "$STUB_LOG"
case "$*" in
  *"--jq .html_url"*) for arg in "$@"; do case "$arg" in body=@*) cat "${arg#body=@}" > "$STUB_BODY" ;; esac; done
    echo "https://example.invalid/pull/1" ;;
  *state=open*) [ -z "${STUB_OPEN_SYNC:-}" ] || echo "$STUB_OPEN_SYNC" ;;
  *) echo "unexpected gh call: $*" >&2; exit 1 ;;
esac
STUB
cat > "$scratch/bin/git" <<STUB
#!/usr/bin/env bash
echo "\$*" >> "\$GIT_LOG"
exec "$(command -v git)" "\$@"
STUB
chmod +x "$scratch/bin/gh" "$scratch/bin/git"
export STUB_LOG="$scratch/gh.log" STUB_BODY="$scratch/body.txt" GIT_LOG="$scratch/git.log" SYNC_REPO=example/fork
: > "$STUB_LOG"; : > "$GIT_LOG"
sync_checkout="$scratch/work"
run_sync() { (cd "$sync_checkout" && PATH="$scratch/bin:$PATH" bash "$here/sync-fork.sh" "$@" 2>&1); }
printf 'audit ok\nVERDICT: PASS\n' > "$scratch/audit.md"
posts() { grep -c -- '--jq .html_url' "$STUB_LOG" || true; }
pushes() { grep -c '^push ' "$GIT_LOG" || true; }

# 1. A missing, empty or verdict-less body file refuses the run before any push or PR.
: > "$scratch/empty.md"; printf 'audit, no verdict\n' > "$scratch/no-verdict.md"
pushes_before="$(pushes)"
for body in "" "$scratch/missing.md" "$scratch/empty.md" "$scratch/no-verdict.md"; do
  code=0; out="$(run_sync "$first_target" ${body:+"$body"})" || code=$?
  [ "$code" -eq 2 ] || { echo "body '${body:-<none>}': exit $code, want 2: $out" >&2; exit 1; }
done
[ "$(pushes)" = "$pushes_before" ] && [ "$(posts)" -eq 0 ] || { echo "a refused run pushed or opened a PR" >&2; exit 1; }

# 2. An open sync PR holds the run and changes nothing on the fork.
fork_before="$(git -C "$scratch/fork.git" rev-parse mine)"
code=0; out="$(STUB_OPEN_SYNC=5 run_sync "$first_target" "$scratch/audit.md")" || code=$?
[ "$code" -eq 1 ] && case "$out" in *"VERDICT: HOLD sync PR #5 not landed yet"*) ;; *) false ;; esac \
  || { echo "an open sync PR did not hold the run (exit $code): $out" >&2; exit 1; }
! git -C "$scratch/fork.git" rev-parse -q --verify "refs/heads/sync/${first_target:0:12}" >/dev/null \
  || { echo "a held run pushed a sync branch" >&2; exit 1; }
[ "$(posts)" -eq 0 ] || { echo "a held run opened a PR" >&2; exit 1; }

# 3. The verbatim promotion merges with no conflict, as one PR, from a branch off origin/mine.
run_sync "$first_target" "$scratch/audit.md" >/dev/null || { echo "the clean sync failed" >&2; exit 1; }
sync_head="$(git -C "$scratch/fork.git" rev-parse "refs/heads/sync/${first_target:0:12}")"
[ "$(git -C "$scratch/fork.git" rev-parse mine)" = "$fork_before" ] || { echo "the sync moved mine" >&2; exit 1; }
[ "$(git -C "$scratch/fork.git" rev-parse "$sync_head^1")" = "$fork_before" ] || { echo "the sync branch is not a merge onto mine" >&2; exit 1; }
[ "$(git -C "$scratch/fork.git" rev-parse "$sync_head^2")" = "$first_target" ] || { echo "the sync branch does not merge the target" >&2; exit 1; }
[ "$(posts)" -eq 1 ] || { echo "want one PR, got $(posts)" >&2; exit 1; }
grep -q -- "base=mine" "$STUB_LOG" && grep -q -- "head=sync/${first_target:0:12}" "$STUB_LOG" \
  || { echo "the PR does not name the sync branch and mine" >&2; exit 1; }
grep -q 'VERDICT: PASS' "$STUB_BODY" || { echo "the PR body lacks the audit summary" >&2; exit 1; }
[ "$(git -C "$scratch/work" branch --show-current)" = work ] || { echo "the sync did not return to the starting branch" >&2; exit 1; }
land() { git -C "$scratch/fork.git" update-ref refs/heads/mine "$(git -C "$scratch/fork.git" rev-parse "refs/heads/sync/${1:0:12}")"; }
land "$first_target"

# 4. A change upstream tweaked conflicts once, is resolved in the merge commit, and the rerun
# pushes and opens the PR.
code=0; out="$(run_sync "$conflict_target" "$scratch/audit.md")" || code=$?
[ "$code" -eq 1 ] && git -C "$scratch/work" rev-parse -q --verify MERGE_HEAD >/dev/null \
  || { echo "the conflicting sync did not stop mid-merge (exit $code): $out" >&2; exit 1; }
git -C "$scratch/work" checkout -q --theirs c.txt && git -C "$scratch/work" add c.txt && commit_as_tester -C "$scratch/work" commit -q --no-edit
run_sync "$conflict_target" "$scratch/audit.md" >/dev/null || { echo "the rerun after the resolution failed" >&2; exit 1; }
[ "$(posts)" -eq 2 ] || { echo "want two PRs in all, got $(posts)" >&2; exit 1; }
land "$conflict_target"
git -C "$scratch/work" fetch -q origin

# 5. The next sync merges cleanly: the resolution is not met again.
printf 'y\n' > "$scratch/upstream-work/d.txt"; commit_as_tester -C "$scratch/upstream-work" commit -q -am "unrelated upstream change"
third_target="$(git -C "$scratch/upstream-work" rev-parse HEAD)"; git -C "$scratch/upstream-work" push -q origin main
run_sync "$third_target" "$scratch/audit.md" >/dev/null || { echo "the sync after the resolution did not merge cleanly" >&2; exit 1; }
[ "$(posts)" -eq 3 ] || { echo "want three PRs in all, got $(posts)" >&2; exit 1; }
land "$third_target"
git -C "$scratch/work" fetch -q origin

# 6. Everything upstream sent is in mine, and mine carries only the fork-only change on top.
git -C "$scratch/fork.git" merge-base --is-ancestor "$third_target" mine || { echo "mine lacks upstream's history" >&2; exit 1; }
only="$(git -C "$scratch/fork.git" diff --name-only main...mine)"
[ "$only" = fork-only.txt ] || { echo "git diff main...mine lists: $only" >&2; exit 1; }

# 7. SYNC_BASE: a scratch base that has no commits of its own past the seed. The target
# fast-forwards onto it, yet a merge commit is made (--no-ff). The PR targets the scratch
# base, the branch starts from it, and mine (which has a commit of its own) is untouched.
git clone -q --bare "$scratch/seed" "$scratch/fork-dry-run.git"
git -C "$scratch/fork-dry-run.git" branch scratch-base main
git -C "$scratch/fork-dry-run.git" branch mine main
git clone -q "$scratch/fork-dry-run.git" "$scratch/dry-run-work"
git -C "$scratch/dry-run-work" remote add upstream "$scratch/upstream.git"
git -C "$scratch/dry-run-work" checkout -q -b work
git -C "$scratch/dry-run-work" checkout -q -b mine-local origin/mine
printf 'mine only\n' > "$scratch/dry-run-work/mine-only.txt"; commit_as_tester -C "$scratch/dry-run-work" add -A; commit_as_tester -C "$scratch/dry-run-work" commit -q -m "mine only"
git -C "$scratch/dry-run-work" push -q origin mine-local:mine
git -C "$scratch/dry-run-work" checkout -q work
mine_before="$(git -C "$scratch/fork-dry-run.git" rev-parse mine)"
scratch_base_tip="$(git -C "$scratch/fork-dry-run.git" rev-parse scratch-base)"
sync_checkout="$scratch/dry-run-work"
SYNC_BASE=scratch-base run_sync "$first_target" "$scratch/audit.md" >/dev/null || { echo "the scratch-base sync failed" >&2; exit 1; }
dry_head="$(git -C "$scratch/fork-dry-run.git" rev-parse "refs/heads/sync/${first_target:0:12}")"
[ "$(git -C "$scratch/fork-dry-run.git" rev-list --parents -n1 "$dry_head" | wc -w)" -eq 3 ] \
  || { echo "the fast-forwardable sync made no merge commit (--no-ff)" >&2; exit 1; }
[ "$(git -C "$scratch/fork-dry-run.git" rev-parse "$dry_head^1")" = "$scratch_base_tip" ] \
  || { echo "the sync branch does not start from the scratch base" >&2; exit 1; }
[ "$(git -C "$scratch/fork-dry-run.git" rev-parse mine)" = "$mine_before" ] || { echo "a scratch-base sync moved mine" >&2; exit 1; }
tail -n1 "$STUB_LOG" | grep -q -- "base=scratch-base" || { echo "the PR does not target the scratch base: $(tail -n1 "$STUB_LOG")" >&2; exit 1; }
sync_checkout="$scratch/work"

# 8. No sync pushed with a force option (--force and its variants, -f, a +refspec), and the sync branches went up.
! grep -Eq '^push .*(--force| -f( |$)| \+)' "$GIT_LOG" || { echo "a sync forced a push: $(grep -E '^push .*(--force| -f( |$)| \+)' "$GIT_LOG")" >&2; exit 1; }
grep -q "^push origin sync/${first_target:0:12}\$" "$GIT_LOG" || { echo "the sync branch push was not observed" >&2; exit 1; }
echo "sync-fork.sh: merge PR flow ok"
