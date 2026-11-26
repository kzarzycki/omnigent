#!/usr/bin/env bash
# run-affected-tests.sh with a fake selector and a fake uv: pytest gets the per-test cap, a hung run
# ends at the wall-clock limit with a nonzero exit, and the record (failed-tests.json, what `rerun-tests` reads)
# is written on a pass, a failure and a timeout.
set -u
here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../../.." && pwd)
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
mkdir "$tmp/bin"
# The selector prints one file, or nothing (SELECTS_NOTHING), or fails (FULL_FAILS).
cat > "$tmp/bin/python3" <<EOF
#!/bin/bash
case "\$1" in
  *affected_tests.py)
    [ -z "\${FULL_FAILS:-}" ] || exit 3
    if [ -n "\${WHOLE_SUITE:-}" ]; then echo tests; elif [ -z "\${SELECTS_NOTHING:-}" ]; then echo tests/example_test.py; fi ;;
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
# The gate runs this test with its own record and shard variables set; each case sets only its own.
affected() {
  rm -f "$tmp/args"
  env -u CI -u AFFECTED_TESTS_FULL -u AFFECTED_TESTS_RECORD -u AFFECTED_TESTS_SHARDS -u AFFECTED_TESTS_SHARD RECEIVED="$tmp/args" PATH="$tmp/bin:$PATH" "$@" \
    bash "$here/run-affected-tests.sh" >"$tmp/out" 2>&1
}

# Without a record, pytest gets today's arguments exactly.
affected; rc=$?
[ "$rc" = 0 ] || fail "a pass exited $rc"
printf '%s\n' run --no-sync pytest -n auto --dist=loadfile --max-worker-restart=0 --timeout=90 tests/example_test.py > "$tmp/expected"
cmp -s "$tmp/expected" "$tmp/args" || fail "arguments without a record changed: $(cat "$tmp/args")"
affected HANG=1 AFFECTED_TESTS_WALL_SECONDS=1; rc=$?
[ "$rc" = 124 ] || fail "a hung run exited $rc, not 124"

# A shard runs its slice of the selection, and one holding no selected test passes (pytest's exit 5).
affected AFFECTED_TESTS_SHARDS=4 AFFECTED_TESTS_SHARD=2; rc=$?
[ "$rc" = 0 ] || fail "a shard exited $rc"
printf '%s\n' run --no-sync pytest -n auto --dist=loadfile --max-worker-restart=0 --timeout=90 --num-shards=4 --shard-id=2 tests/example_test.py > "$tmp/expected"
cmp -s "$tmp/expected" "$tmp/args" || fail "shard arguments: $(cat "$tmp/args")"
affected AFFECTED_TESTS_SHARDS=4 AFFECTED_TESTS_SHARD=2 PYTEST_EXIT=5; rc=$?
[ "$rc" = 0 ] || fail "an empty shard exited $rc, not 0"
affected PYTEST_EXIT=5; rc=$?
[ "$rc" = 5 ] || fail "an unsharded run that collected nothing exited $rc, not 5"
affected AFFECTED_TESTS_SHARDS=4 PYTEST_EXIT=0; rc=$?
[ "$rc" != 0 ] || fail "shards without a shard number ran"

# record <outcome> <expected failed-tests.json>: the record holds the failed tests of pytest's result.
record() {
  [ "$(cat "$tmp/record/failed-tests.json")" = "$2" ] || fail "$1: record holds $(cat "$tmp/record/failed-tests.json")"
  [ "$(ls "$tmp/record")" = failed-tests.json ] || fail "$1: the record holds more than failed-tests.json: $(ls "$tmp/record")"
}
affected AFFECTED_TESTS_RECORD="$tmp/record"; rc=$?
[ "$rc" = 0 ] || fail "a recorded pass exited $rc"
record pass '{"failed": []}'
affected AFFECTED_TESTS_RECORD="$tmp/record" FAILED=tests/example_test.py::test_b PYTEST_EXIT=1; rc=$?
[ "$rc" = 1 ] || fail "a recorded failure exited $rc, not 1"
record failure '{"failed": ["tests/example_test.py::test_b"]}'
affected AFFECTED_TESTS_RECORD="$tmp/record" HANG=1 AFFECTED_TESTS_WALL_SECONDS=1; rc=$?
[ "$rc" = 124 ] || fail "a recorded hung run exited $rc, not 124"
record timeout '{"failed": []}'
# Nothing selected: a pass with exit 0; a selector that dies leaves no record from an earlier run.
affected AFFECTED_TESTS_RECORD="$tmp/record" SELECTS_NOTHING=1; rc=$?
[ "$rc" = 0 ] && [ "$(cat "$tmp/record/failed-tests.json")" = '{"failed": []}' ] && [ ! -e "$tmp/args" ] || fail "an empty selection exited $rc or ran pytest"
affected AFFECTED_TESTS_RECORD="$tmp/record" FULL_FAILS=1; rc=$?
[ "$rc" != 0 ] && [ ! -e "$tmp/record/failed-tests.json" ] || fail "a failed selector exited $rc and left a record"
# A worker that SIGKILLs itself, with real pytest and the gate's flags on a two-worker generated suite: the run ends
# at once with exit 70 (not pytest's 1, so a record never reads it as complete), names the worker and test, and
# runs the killing test once (a restarted worker would run it again). Without the flag the same suite exits 1.
mkdir "$tmp/suite" "$tmp/realbin"
printf '#!/bin/bash\ncase "$1" in *affected_tests.py) echo "%s" ;; *) exec %s "$@" ;; esac\n' "$tmp/suite" "$(command -v python3)" > "$tmp/realbin/python3"
chmod +x "$tmp/realbin/python3"
cat > "$tmp/suite/test_crash.py" <<EOF
import os, signal
def test_kill():
    open("$tmp/kills", "a").write("x\n")
    os.kill(os.getpid(), signal.SIGKILL)
EOF
printf 'def test_other():\n    pass\n' > "$tmp/suite/test_other.py"
real() { env -u CI -u AFFECTED_TESTS_FULL -u AFFECTED_TESTS_RECORD -u AFFECTED_TESTS_SHARDS -u AFFECTED_TESTS_SHARD AFFECTED_TESTS_WORKERS=2 PATH="$tmp/realbin:$PATH" "$@" \
  bash "$here/run-affected-tests.sh" >"$tmp/real.out" 2>&1; }
: > "$tmp/kills"; start=$SECONDS
real AFFECTED_TESTS_RECORD="$tmp/record"; rc=$?
[ "$rc" = 70 ] || fail "a crashed worker exited $rc, not 70: $(tail -5 "$tmp/real.out")"
[ $((SECONDS - start)) -lt 60 ] || fail "a crashed worker took $((SECONDS - start)) s to end the run"
grep -q "worker 'gw[0-9]*' crashed while running 'test_crash.py::test_kill'" "$tmp/real.out" || fail "the crash names no worker and test"
kills=$(wc -l < "$tmp/kills")  # BSD wc pads the count with spaces: compare it as a number
[ "$kills" -eq 1 ] || fail "the killing test ran $kills times: the worker was restarted"
[ -f "$tmp/record/failed-tests.json" ] || fail "a crashed run left no record: $(tail -5 "$tmp/real.out")"
# A whole-suite selection, unsharded and outside CI, is skipped (record: nothing failed); a shard, CI
# or AFFECTED_TESTS_FULL=1 runs it.
line="affected tests: whole suite selected; the merge queue's sharded gate runs it"
affected WHOLE_SUITE=1 AFFECTED_TESTS_RECORD="$tmp/record" PYTEST_EXIT=1; rc=$?
[ "$rc" = 0 ] && [ ! -e "$tmp/args" ] && [ "$(cat "$tmp/out")" = "$line" ] || fail "a whole-suite selection exited $rc or ran pytest: $(cat "$tmp/out")"
[ "$(cat "$tmp/record/failed-tests.json")" = '{"failed": []}' ] || fail "the skip's record is wrong"
affected WHOLE_SUITE=1 AFFECTED_TESTS_FULL=1; rc=$?
[ "$rc" = 0 ] && [ "$(tail -1 "$tmp/args")" = tests ] || fail "AFFECTED_TESTS_FULL=1 didn't run the suite"
affected WHOLE_SUITE=1 CI=true; rc=$?
[ "$rc" = 0 ] && [ "$(tail -1 "$tmp/args")" = tests ] || fail "a CI run skipped the suite"
affected WHOLE_SUITE=1 AFFECTED_TESTS_SHARDS=4 AFFECTED_TESTS_SHARD=1; rc=$?
[ "$rc" = 0 ] && [ "$(tail -1 "$tmp/args")" = tests ] || fail "a shard skipped the suite"
echo "cloud-dev run-affected-tests.sh: ok"
