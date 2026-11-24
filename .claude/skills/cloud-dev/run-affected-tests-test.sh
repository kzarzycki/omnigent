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
    [ -n "\${SELECTS_NOTHING:-}" ] || echo tests/example_test.py ;;
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
  env -u AFFECTED_TESTS_RECORD RECEIVED="$tmp/args" PATH="$tmp/bin:$PATH" "$@" \
    bash "$here/run-affected-tests.sh" >"$tmp/out" 2>&1
}

# Without a record, pytest gets today's arguments exactly.
affected; rc=$?
[ "$rc" = 0 ] || fail "a pass exited $rc"
printf '%s\n' run --no-sync pytest -n auto --dist=loadfile --timeout=90 tests/example_test.py > "$tmp/expected"
cmp -s "$tmp/expected" "$tmp/args" || fail "arguments without a record changed: $(cat "$tmp/args")"
affected HANG=1 AFFECTED_TESTS_WALL_SECONDS=1; rc=$?
[ "$rc" = 124 ] || fail "a hung run exited $rc, not 124"

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
echo "cloud-dev run-affected-tests.sh: ok"
