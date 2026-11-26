#!/usr/bin/env bash
# The record round trip with real pytest, in a scratch repo holding the real run-affected-tests.sh:
# run 1 fails on a planted test and writes a record; after a fix to only that test, run 2 builds on
# the record, runs only that test and passes. Then a test that fails once: run 3 fails on it, run 4
# reruns the whole selection (nothing changed reselects it), passes, and records it as a flake.
# Then a test that fails only after another test: run 5 fails on it, and run 6 keeps it red.
set -euo pipefail
repo=$(cd "$(dirname "$0")/../.." && pwd)
export UV_PROJECT_ENVIRONMENT=${UV_PROJECT_ENVIRONMENT:-$repo/.venv}
unset AFFECTED_TESTS_SINCE AFFECTED_TESTS_RECORD
scratch=$(mktemp -d); trap 'rm -rf "$scratch"' EXIT
mkdir -p "$scratch/repo/.claude/skills/cloud-dev" "$scratch/repo/tests/planted" "$scratch/records"
cd "$scratch/repo"
git init -q -b main
git config user.email gate@example.invalid
git config user.name gate
cp "$repo/.claude/skills/cloud-dev/run-affected-tests.sh" "$repo/.claude/skills/cloud-dev/affected_tests.py" \
  .claude/skills/cloud-dev/
printf '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n' > pyproject.toml
printf '__pycache__/\nsnapshot_failures/\n' > .gitignore
git add -A && git commit -qm base
export GATE_BASE
GATE_BASE=$(git rev-parse HEAD)
fail() { echo "FAIL: $*"; exit 1; }
# gate <n> [<record it builds on>]: one run-affected-tests.sh run, its log printed; sets rc
gate() {
  rc=0
  AFFECTED_TESTS_RECORD="$scratch/records/run$1" AFFECTED_TESTS_SINCE=${2:+$scratch/records/$2} \
    bash .claude/skills/cloud-dev/run-affected-tests.sh > "$scratch/run$1.log" 2>&1 || rc=$?
  echo "== run $1 (exit $rc)"
  cat "$scratch/run$1.log"
}

# the change adds two tests; one is wrong
printf 'def test_right():\n    assert 1 + 1 == 2\n' > tests/planted/test_right.py
printf 'def test_wrong():\n    assert 1 + 1 == 3\n' > tests/planted/test_wrong.py
git add -A && git commit -qm change
gate 1
[ "$rc" = 1 ] || fail "run 1 exited $rc, not 1"
grep -q '1 failed, 1 passed' "$scratch/run1.log" || fail "run 1 didn't run both tests"
[ "$(cat "$scratch/records/run1/exit")" = 1 ] || fail "run 1's record holds exit $(cat "$scratch/records/run1/exit")"
grep -q '"tests/planted/test_wrong.py::test_wrong"' "$scratch/records/run1/lastfailed.json" ||
  fail "run 1's record doesn't name the failed test"

# the fix touches only the wrong test: run 2 runs it alone
printf 'def test_wrong():\n    assert 1 + 1 == 2\n' > tests/planted/test_wrong.py
git commit -qam fix
gate 2 run1
[ "$rc" = 0 ] || fail "run 2 exited $rc, not 0"
grep -qx 'affected tests: tests/planted/test_wrong.py' "$scratch/run2.log" || fail "run 2 ran more than the fixed test"
grep -q '1 passed' "$scratch/run2.log" && ! grep -q test_right "$scratch/run2.log" || fail "run 2 didn't run only the fixed test"

# a test that fails once, then passes with nothing changed: a flake
printf 'import os, pathlib\n\n\ndef test_once():\n    mark = pathlib.Path(os.environ["ONCE_MARK"])\n    if not mark.exists():\n        mark.touch()\n        raise AssertionError("fails the first time only")\n' \
  > tests/planted/test_once.py
git add -A && git commit -qm once
export ONCE_MARK=$scratch/once-ran
gate 3 run2
[ "$rc" = 1 ] && grep -q '1 failed' "$scratch/run3.log" || fail "run 3 didn't fail on the new test alone"
gate 4 run3
[ "$rc" = 0 ] || fail "run 4 exited $rc, not 0"
grep -q '3 passed' "$scratch/run4.log" || fail "run 4 didn't rerun the whole selection"
[ "$(cat "$scratch/records/run4/flakes")" = tests/planted/test_once.py::test_once ] || fail "run 4 recorded no flake"

# a test that passes alone and fails after the test before it (state left behind): never a flake
printf 'import os\n\n\ndef test_leaves_state():\n    os.environ["LEFT_OVER"] = "1"\n\n\ndef test_needs_clean_state():\n    assert "LEFT_OVER" not in os.environ\n' \
  > tests/planted/test_order.py
git add -A && git commit -qm order
gate 5 run4
[ "$rc" = 1 ] && grep -q '1 failed' "$scratch/run5.log" || fail "run 5 didn't fail on the order-dependent test"
gate 6 run5
[ "$rc" = 1 ] || fail "run 6 exited $rc: an order-dependent failure went green"
grep -q '1 failed, 4 passed' "$scratch/run6.log" || fail "run 6 didn't rerun the whole selection"
[ ! -s "$scratch/records/run6/flakes" ] || fail "run 6 called the order-dependent test a flake"
echo "PASS: a fix reran only the fixed test; a failure nothing changed reran the whole selection, which recorded a passing one as a flake and kept an order-dependent one red"
