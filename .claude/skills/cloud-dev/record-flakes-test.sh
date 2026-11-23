#!/usr/bin/env bash
# record-flakes.sh with a fake gh: a flake is commented on its open `Flaky:` issue or gets a new one, both
# naming the files that differ between the two trees; one whose call fails is recorded as failed instead.
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
# listing <path=blob>...: a `git ls-tree -r -z` listing
listing() { for entry; do printf '100644 blob %s\t%s\0' "${entry#*=}" "${entry%%=*}"; done; }
# setup <earlier blob of pkg/core.py> <this one>: two records, this one with three flakes from a run that passed
setup() {
  rm -rf "$tmp/earlier" "$tmp/record" "$tmp/calls" "$tmp/summary"; mkdir "$tmp/earlier" "$tmp/record"
  listing "pkg/core.py=$1" tests/a.py=bbb > "$tmp/earlier/tree"; listing "pkg/core.py=$2" tests/a.py=bbb > "$tmp/record/tree"
  printf 'tests/a.py::t\ntests/b.py::u v\ntests/c.py::w [x y]\n' > "$tmp/record/flakes"
  echo '{}' > "$tmp/record/lastfailed.json"; echo 0 > "$tmp/record/exit"
}
flakes() {
  env PATH="$tmp/bin:$PATH" GH_CALLS="$tmp/calls" GITHUB_STEP_SUMMARY="$tmp/summary" GH_REPO=o/r \
    EARLIER_RUN=https://github.example/run/1 EARLIER_SHA=sha1 PR=5 HEAD_SHA=sha2 THIS_RUN=https://github.example/run/2 "$@" \
    bash "$here/record-flakes.sh" "$tmp/record" "$tmp/earlier" > "$tmp/out" 2>&1
}
setup aaa ccc
flakes; rc=$?
[ "$rc" = 0 ] || fail "filing exited $rc: $(cat "$tmp/out")"
grep -q '^api repos/o/r/issues/42/comments -f body=.*Files that differ between the two runs.* trees: pkg/core\.py\.' "$tmp/calls" ||
  fail "no comment on the open issue naming the differing file: $(cat "$tmp/calls")"
grep -q '^api repos/o/r/issues/7/comments ' "$tmp/calls" || fail "a described title wasn't matched"
grep -q '^api repos/o/r/issues -f title=Flaky: tests/b.py::u v -f body=.* -f labels\[\]=bug' "$tmp/calls" ||
  fail "no new issue for the unmatched flake"
[ "$(grep -c '^- `' "$tmp/summary")" = 3 ] || fail "the summary doesn't list the three flakes: $(cat "$tmp/summary")"
[ "$(cat "$tmp/record/lastfailed.json")" = '{}' ] || fail "a filed flake was recorded as failed"
setup aaa aaa
flakes; grep -q 'trees: none\.' "$tmp/calls" || fail "an unchanged tree isn't named as none"
# a failed call: that flake is recorded as failed, the others are filed, and the run is red
setup aaa aaa
flakes GH_FAILS=create; rc=$?
[ "$rc" = 1 ] || fail "a failed issue call exited $rc"
[ "$(cat "$tmp/record/lastfailed.json")" = '{"tests/b.py::u v": true}' ] && [ "$(cat "$tmp/record/exit")" = 1 ] ||
  fail "the unfiled flake isn't recorded as failed: $(cat "$tmp/record/lastfailed.json") exit $(cat "$tmp/record/exit")"
grep -q 'tests/b.py::u v`: not filed' "$tmp/summary" || fail "the summary doesn't say the flake wasn't filed"
setup aaa aaa
flakes GH_FAILS=comment; rc=$?
[ "$rc" = 1 ] && [ "$(cat "$tmp/record/exit")" = 1 ] || fail "a failed comment call exited $rc"
[ "$(cat "$tmp/record/lastfailed.json")" = '{"tests/a.py::t": true, "tests/c.py::w [x y]": true}' ] ||
  fail "the uncommented flakes aren't recorded as failed: $(cat "$tmp/record/lastfailed.json")"
grep -q 'tests/b.py::u v`: https://github.example/o/r/issues/77' "$tmp/summary" || fail "the created issue wasn't filed"
setup aaa aaa
flakes GH_FAILS=list; rc=$?
[ "$rc" = 1 ] && [ "$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))))' "$tmp/record/lastfailed.json")" = 3 ] ||
  fail "without the issue list every flake should wait for the next run"
grep -q 'comments\|-f title' "$tmp/calls" && fail "issues were written without the issue list"
# no flakes: nothing to do
setup aaa aaa; : > "$tmp/record/flakes"
flakes; [ $? = 0 ] && [ ! -e "$tmp/calls" ] || fail "no flakes still called gh"
echo "cloud-dev record-flakes.sh: ok"
