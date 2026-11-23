#!/usr/bin/env bash
# run-affected-tests.sh with a fake selector and a fake uv: pytest gets the per-test cap, a hung run
# ends at the wall-clock limit with a nonzero exit, and the record is written on a pass, a failure and
# a timeout, with pytest's exit code.
set -u
here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../../.." && pwd)
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
mkdir "$tmp/bin"
# The selector prints one file; with --since, CANDIDATE (default one node id) as the run and its flake candidate.
cat > "$tmp/bin/python3" <<EOF
#!/bin/bash
case "\$1" in
  *affected_tests.py)
    if [ "\${2:-}" = --since ]; then
      [ -z "\${SELECTOR_FAILS:-}" ] || exit 3
      echo "affected tests: flake candidate: \${CANDIDATE:-tests/example_test.py::test_a}" >&2
      echo "\${CANDIDATE:-tests/example_test.py::test_a}"
    else
      [ -z "\${FULL_FAILS:-}" ] || exit 3
      [ -n "\${SELECTS_NOTHING:-}" ] || echo tests/example_test.py
    fi ;;
  *) exec $(command -v python3) "\$@" ;;
esac
EOF
# pytest: records its arguments, writes FAILED into the cache it is given, exits PYTEST_EXIT.
cat > "$tmp/bin/uv" <<'EOF'
#!/bin/bash
printf "%s\n" "$@" > "$RECEIVED"
[ -z "${HANG:-}" ] || exec sleep 30
for arg in "$@"; do case "$arg" in cache_dir=*) cache=${arg#cache_dir=} ;; esac; done
if [ -n "${FAILED:-}" ] && [ -n "${cache:-}" ]; then
  mkdir -p "$cache/v/cache"; printf '{"%s": true}' "$FAILED" > "$cache/v/cache/lastfailed"
fi
exit "${PYTEST_EXIT:-0}"
EOF
chmod +x "$tmp/bin/python3" "$tmp/bin/uv"
fail() { echo "!! affected tests: $*"; exit 1; }
# The gate runs this test with its own record variables set; each case sets only its own.
affected() {
  rm -f "$tmp/args"
  env -u AFFECTED_TESTS_SINCE -u AFFECTED_TESTS_RECORD RECEIVED="$tmp/args" PATH="$tmp/bin:$PATH" "$@" \
    bash "$here/run-affected-tests.sh" >"$tmp/out" 2>&1
}

# Without a record, pytest gets today's arguments exactly.
affected; rc=$?
[ "$rc" = 0 ] || fail "a pass exited $rc"
printf '%s\n' run --no-sync pytest -n auto --dist=loadfile --timeout=90 tests/example_test.py > "$tmp/expected"
cmp -s "$tmp/expected" "$tmp/args" || fail "arguments without a record changed: $(cat "$tmp/args")"
affected HANG=1 AFFECTED_TESTS_WALL_SECONDS=1; rc=$?
[ "$rc" = 124 ] || fail "a hung run exited $rc, not 124"

# record <outcome> <expected exit> <expected lastfailed>: the record holds the tree, the selection and pytest's result.
record() {
  [ "$(cat "$tmp/record/exit")" = "$2" ] || fail "$1: record exit $(cat "$tmp/record/exit"), not $2"
  [ "$(cat "$tmp/record/lastfailed.json")" = "$3" ] || fail "$1: record lastfailed $(cat "$tmp/record/lastfailed.json")"
  [ "$(cat "$tmp/record/selected")" = tests/example_test.py ] || fail "$1: record selected $(cat "$tmp/record/selected")"
  git -C "$root" ls-tree -r -z HEAD | cmp -s - "$tmp/record/tree" || fail "$1: record tree is not HEAD's listing"
}
affected AFFECTED_TESTS_RECORD="$tmp/record"; rc=$?
[ "$rc" = 0 ] || fail "a recorded pass exited $rc"
record pass 0 '{}'
affected AFFECTED_TESTS_RECORD="$tmp/record" FAILED=tests/example_test.py::test_b PYTEST_EXIT=1; rc=$?
[ "$rc" = 1 ] || fail "a recorded failure exited $rc, not 1"
record failure 1 '{"tests/example_test.py::test_b": true}'
affected AFFECTED_TESTS_RECORD="$tmp/record" HANG=1 AFFECTED_TESTS_WALL_SECONDS=1; rc=$?
[ "$rc" = 124 ] || fail "a recorded hung run exited $rc, not 124"
record timeout 124 '{}'

# Building on a record: the selector's run list runs; a candidate that passes is a flake, one that fails is not.
mkdir "$tmp/since"
affected AFFECTED_TESTS_SINCE="$tmp/since" AFFECTED_TESTS_RECORD="$tmp/record"; rc=$?
[ "$rc" = 0 ] || fail "a rerun that passed exited $rc"
[ "$(tail -1 "$tmp/args")" = tests/example_test.py::test_a ] || fail "the rerun ran $(tail -1 "$tmp/args")"
[ "$(cat "$tmp/record/flakes")" = tests/example_test.py::test_a ] || fail "no flake recorded: $(cat "$tmp/out")"
[ "$(cat "$tmp/record/selected")" = tests/example_test.py ] || fail "a rerun's record holds $(cat "$tmp/record/selected"), not the full selection"
grep -qx 'affected tests: flaky, passed on the rerun: tests/example_test.py::test_a' "$tmp/out" || fail "no flake in the log"
affected AFFECTED_TESTS_SINCE="$tmp/since" AFFECTED_TESTS_RECORD="$tmp/record" FAILED=tests/example_test.py::test_a PYTEST_EXIT=1; rc=$?
[ "$rc" = 1 ] || fail "a rerun whose failed test failed again exited $rc, not 1"
[ ! -s "$tmp/record/flakes" ] || fail "a test that failed again was called a flake"
affected AFFECTED_TESTS_SINCE="$tmp/since" AFFECTED_TESTS_RECORD="$tmp/record" FAILED=tests/example_test.py::test_c PYTEST_EXIT=1; rc=$?
[ "$rc" = 1 ] && [ "$(cat "$tmp/record/flakes")" = tests/example_test.py::test_a ] ||
  fail "a passing candidate in a failed rerun exited $rc and recorded flakes '$(cat "$tmp/record/flakes")'"
# A file that failed to collect is no flake while a test in it fails, nor a test while its file fails to collect.
affected AFFECTED_TESTS_SINCE="$tmp/since" AFFECTED_TESTS_RECORD="$tmp/record" CANDIDATE=tests/example_test.py \
  FAILED=tests/example_test.py::test_x PYTEST_EXIT=1; rc=$?
[ "$rc" = 1 ] && [ ! -s "$tmp/record/flakes" ] || fail "a file whose test failed was called a flake: $(cat "$tmp/record/flakes")"
affected AFFECTED_TESTS_SINCE="$tmp/since" AFFECTED_TESTS_RECORD="$tmp/record" FAILED=tests/example_test.py PYTEST_EXIT=1; rc=$?
[ "$rc" = 1 ] && [ ! -s "$tmp/record/flakes" ] || fail "a test whose file failed to collect was called a flake"
# Building on a record without writing one still reads this run's own failures.
affected AFFECTED_TESTS_SINCE="$tmp/since" FAILED=tests/example_test.py::test_a PYTEST_EXIT=1; rc=$?
[ "$rc" = 1 ] && ! grep -q 'flaky, passed on the rerun' "$tmp/out" || fail "a rerun without a record called a failing test a flake"
affected AFFECTED_TESTS_SINCE="$tmp/since" AFFECTED_TESTS_RECORD="$tmp/record" HANG=1 AFFECTED_TESTS_WALL_SECONDS=1; rc=$?
[ "$rc" = 124 ] && [ ! -s "$tmp/record/flakes" ] || fail "a timed-out rerun exited $rc or named a flake"
affected AFFECTED_TESTS_SINCE="$tmp/since" SELECTOR_FAILS=1; rc=$?
[ "$rc" = 0 ] && [ "$(tail -1 "$tmp/args")" = tests/example_test.py ] || fail "a failed selector didn't run the full selection"
# Nothing selected: a pass with exit 0; a selector that dies leaves no exit code from an earlier run.
affected AFFECTED_TESTS_RECORD="$tmp/record" SELECTS_NOTHING=1; rc=$?
[ "$rc" = 0 ] && [ "$(cat "$tmp/record/exit")" = 0 ] && [ ! -e "$tmp/args" ] || fail "an empty selection exited $rc or ran pytest"
affected AFFECTED_TESTS_RECORD="$tmp/record" FULL_FAILS=1; rc=$?
[ "$rc" != 0 ] && [ ! -e "$tmp/record/exit" ] || fail "a failed selector exited $rc and left exit $(cat "$tmp/record/exit")"
echo "cloud-dev run-affected-tests.sh: ok"
