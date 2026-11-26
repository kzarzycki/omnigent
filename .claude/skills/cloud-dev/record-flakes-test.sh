#!/usr/bin/env bash
# record-flakes.sh with a fake gh, on the output of `approvals.py rerun-tests`: a flake candidate of a green rerun
# is commented on its open `Flaky:` issue or gets a new one; one whose call fails is recorded as failed instead.
set -u
here=$(cd "$(dirname "$0")" && pwd)
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
mkdir "$tmp/bin"
# gh: lists one open issue (#42, `Flaky: tests/a.py::t`); comments and creates print a URL, or fail on GH_FAILS
cat > "$tmp/bin/gh" <<'EOF'
#!/bin/bash
printf '%s\n' "$*" >> "$GH_CALLS"
case "${GH_FAILS:-}:$*" in
  list:*"--paginate"*|comment:*"/comments"*|create:*"repos/o/r/issues -f"*) echo "HTTP 502" >&2; exit 1 ;;
esac
case "$*" in
  *"--paginate"*) printf '42\tFlaky: tests/a.py::t\n7\tFlaky: tests/c.py::w [x y] hangs\n' ;;
  *"issues/42/comments"*) echo "https://github.example/o/r/issues/42#issuecomment-1" ;;
  *"issues/7/comments"*) echo "https://github.example/o/r/issues/7#issuecomment-2" ;;
  "api repos/o/r/issues -f"*) echo "https://github.example/o/r/issues/77" ;;
  *) echo "unexpected gh $*" >&2; exit 9 ;;
esac
EOF
chmod +x "$tmp/bin/gh"
fail() { echo "!! record-flakes: $*"; exit 1; }
# setup [green]: this run's record, and the rerun-tests output: three flake candidates, the green line unless "red"
setup() {
  rm -rf "$tmp/record" "$tmp/calls" "$tmp/summary"; mkdir "$tmp/record"
  { echo "rerun-tests: nothing changed reselects 3 failed test(s), so the red run's whole selection runs"
    printf 'rerun-tests: flake candidate: %s\n' tests/a.py::t "tests/b.py::u v" "tests/c.py::w [x y]" tests/a.py::t
    if [ "${1:-green}" = green ]; then echo "rerun-tests: green: a flake candidate that passed is a flake: open or update its \`Flaky:\` issue"
    else echo "rerun-tests: red again on the same selection: the change caused it"; fi; } > "$tmp/rerun.log"
  echo '{"failed": []}' > "$tmp/record/failed-tests.json"
}
flakes() {
  env PATH="$tmp/bin:$PATH" GH_CALLS="$tmp/calls" GITHUB_STEP_SUMMARY="$tmp/summary" GH_REPO=o/r \
    EARLIER_RUN=https://github.example/run/1 EARLIER_SHA=sha1 PR=5 HEAD_SHA=sha2 THIS_RUN=https://github.example/run/2 "$@" \
    bash "$here/record-flakes.sh" "$tmp/rerun.log" "$tmp/record" > "$tmp/out" 2>&1
}
setup
flakes; rc=$?
[ "$rc" = 0 ] || fail "filing exited $rc: $(cat "$tmp/out")"
grep -q '^api repos/o/r/issues/42/comments -f body=Failed in https://github.example/run/1 (head sha1) and passed on the rerun in https://github.example/run/2 (head sha2), for #5\.' "$tmp/calls" ||
  fail "no comment on the open issue: $(cat "$tmp/calls")"
[ "$(grep -c 'issues/42/comments' "$tmp/calls")" = 1 ] || fail "a repeated candidate was filed twice"
grep -q '^api repos/o/r/issues/7/comments ' "$tmp/calls" || fail "a described title wasn't matched"
grep -q '^api repos/o/r/issues -f title=Flaky: tests/b.py::u v -f body=.* -f labels\[\]=bug' "$tmp/calls" ||
  fail "no new issue for the unmatched flake"
[ "$(grep -c '^- `' "$tmp/summary")" = 3 ] || fail "the summary doesn't list the three flakes: $(cat "$tmp/summary")"
[ "$(cat "$tmp/record/failed-tests.json")" = '{"failed": []}' ] || fail "a filed flake was recorded as failed"
# a failed call: that flake is recorded as failed, the others are filed, and the run is red
setup; echo '{"failed": ["tests/z.py::pre"]}' > "$tmp/record/failed-tests.json"  # a failure of this run itself stays
flakes GH_FAILS=create; rc=$?
[ "$rc" = 1 ] || fail "a failed issue call exited $rc"
[ "$(cat "$tmp/record/failed-tests.json")" = '{"failed": ["tests/b.py::u v", "tests/z.py::pre"]}' ] ||
  fail "the unfiled flake isn't recorded as failed: $(cat "$tmp/record/failed-tests.json")"
grep -q 'tests/b.py::u v`: not filed' "$tmp/summary" || fail "the summary doesn't say the flake wasn't filed"
setup
flakes GH_FAILS=comment; rc=$?
[ "$rc" = 1 ] || fail "a failed comment call exited $rc"
[ "$(cat "$tmp/record/failed-tests.json")" = '{"failed": ["tests/a.py::t", "tests/c.py::w [x y]"]}' ] ||
  fail "the uncommented flakes aren't recorded as failed: $(cat "$tmp/record/failed-tests.json")"
grep -q 'tests/b.py::u v`: https://github.example/o/r/issues/77' "$tmp/summary" || fail "the created issue wasn't filed"
setup
flakes GH_FAILS=list; rc=$?
[ "$rc" = 1 ] && [ "$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))["failed"]))' "$tmp/record/failed-tests.json")" = 3 ] ||
  fail "without the issue list every flake should wait for the next run"
grep -q 'comments\|-f title' "$tmp/calls" && fail "issues were written without the issue list"
# candidates of a rerun that was red again are no flakes; neither is a run with no candidates
setup red
flakes; [ $? = 0 ] && [ ! -e "$tmp/calls" ] || fail "a red rerun's candidates were filed"
setup; echo "rerun-tests: green: nothing" > "$tmp/rerun.log"
flakes; [ $? = 0 ] && [ ! -e "$tmp/calls" ] || fail "no candidates still called gh"
echo "cloud-dev record-flakes.sh: ok"
