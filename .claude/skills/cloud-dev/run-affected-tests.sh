#!/usr/bin/env bash
# Run pytest on the tests the change touches (selector: affected_tests.py).
# Shared by `mise run check-fast` and the fork CI so both run the same line.
# AFFECTED_TESTS_SHARDS / AFFECTED_TESTS_SHARD: run only this shard (0-based) of that many, a hash partition of the
# selected tests by node id (pytest-shard), so a test lands in the same shard whatever else is selected.
# AFFECTED_TESTS_RECORD: a directory this run writes failed-tests.json into ({"failed": [...]}, what
# `approvals.py rerun-tests` reads of a red run), whatever pytest's outcome.
# A whole-suite selection (`tests`) in an unsharded run outside CI is skipped: the merge queue's sharded gate runs
# it (about 18 min, against hours on one worker). AFFECTED_TESTS_FULL=1 forces the local run.
set -euo pipefail
cd "$(dirname "$0")/../../.."
selector=.claude/skills/cloud-dev/affected_tests.py
record=${AFFECTED_TESTS_RECORD:-}
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
# failed-tests.json is written only once the run ends, so a run that dies early leaves nothing to rerun from.
[ -z "$record" ] || { mkdir -p "$record" && rm -f "$record/failed-tests.json"; }
targets=$(python3 "$selector")
run=$targets
shards=${AFFECTED_TESTS_SHARDS:-1}
if [ "$targets" = tests ] && [ "$shards" -le 1 ] && [ -z "${CI:-}" ] && [ "${AFFECTED_TESTS_FULL:-}" != 1 ]; then
  echo "affected tests: whole suite selected; the merge queue's sharded gate runs it"
  [ -z "$record" ] || echo '{"failed": []}' > "$record/failed-tests.json"
  exit 0
fi
if [ -z "$run" ]; then
  echo "affected tests: none touched"
  [ -z "$record" ] || echo '{"failed": []}' > "$record/failed-tests.json"
  exit 0
fi
echo "affected tests: $run"
# One argument per line, so a node id with spaces stays whole; a fresh cache holds only this run's failures.
set --
[ -z "$record" ] || set -- -o "cache_dir=$work/cache"
[ "$shards" -le 1 ] || set -- "$@" --num-shards="$shards" --shard-id="${AFFECTED_TESTS_SHARD:?AFFECTED_TESTS_SHARDS needs AFFECTED_TESTS_SHARD}"
while IFS= read -r target; do set -- "$@" "$target"; done <<< "$run"
# Per-test cap: the slowest passing test took 38 s on a loaded 4-CPU VM, so 90 s ends a hang early (pyproject's
# 300 s stays the default). The wall clock ends a run no test cap can, above a whole-suite run (~200 min on CI's 2 workers).
# --max-worker-restart=0: a dead xdist worker stops the session at once; a restarted worker under loadfile can idle forever.
# macOS has no GNU timeout (coreutils' gtimeout stands in); without either the run has no wall clock.
wall=$(command -v timeout || command -v gtimeout || true)
[ -n "$wall" ] && wall="$wall --kill-after=60 ${AFFECTED_TESTS_WALL_SECONDS:-9000}"
rc=0
# shellcheck disable=SC2086 # $wall is empty or a command and its flags
OMNIGENT_SKIP_WEB_UI=true $wall \
  uv run --no-sync pytest -n "${AFFECTED_TESTS_WORKERS:-auto}" --dist=loadfile --max-worker-restart=0 \
  --timeout="${AFFECTED_TESTS_TIMEOUT:-90}" "$@" | tee "$work/pytest.log" || rc=$?
# A crash makes pytest exit 1 like a failed test (5 when it hits during collection), but the run is incomplete:
# exit 70 keeps it from reading as a full one. This runs before the empty-shard rule below.
if [ "$rc" != 0 ] && grep -q 'worker .* crashed and worker restarting disabled' "$work/pytest.log"; then
  echo "affected tests: an xdist worker crashed, so the run stopped early (see 'crashed while running' above); exit 70" >&2
  rc=70
fi
# A shard the selection puts no test in is a pass (pytest exits 5 when it collects nothing).
[ "$shards" -le 1 ] || [ "$rc" != 5 ] || rc=0
if [ -n "$record" ]; then
  python3 - "$work/cache/v/cache/lastfailed" "$record/failed-tests.json" <<'PY'
import json, os, sys
path = sys.argv[1]
failed = sorted(json.load(open(path))) if os.path.isfile(path) and os.path.getsize(path) else []
json.dump({"failed": failed}, open(sys.argv[2], "w"))
PY
fi
exit "$rc"
