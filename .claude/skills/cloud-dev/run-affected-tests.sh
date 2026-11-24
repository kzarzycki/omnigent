#!/usr/bin/env bash
# Run pytest on the tests the change touches (selector: affected_tests.py).
# Shared by `mise run check-fast` and the fork CI so both run the same line.
# AFFECTED_TESTS_RECORD: a directory this run writes failed-tests.json into ({"failed": [...]}, what
# `approvals.py rerun-tests` reads of a red run), whatever pytest's outcome.
set -euo pipefail
cd "$(dirname "$0")/../../.."
selector=.claude/skills/cloud-dev/affected_tests.py
record=${AFFECTED_TESTS_RECORD:-}
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
# failed-tests.json is written only once the run ends, so a run that dies early leaves nothing to rerun from.
[ -z "$record" ] || { mkdir -p "$record" && rm -f "$record/failed-tests.json"; }
run=$(python3 "$selector")
if [ -z "$run" ]; then
  echo "affected tests: none touched"
  [ -z "$record" ] || echo '{"failed": []}' > "$record/failed-tests.json"
  exit 0
fi
echo "affected tests: $run"
# One argument per line, so a node id with spaces stays whole; a fresh cache holds only this run's failures.
set --
[ -z "$record" ] || set -- -o "cache_dir=$work/cache"
while IFS= read -r target; do set -- "$@" "$target"; done <<< "$run"
# Per-test cap: the slowest passing test took 38 s on a loaded 4-CPU VM, so 90 s ends a hang early (pyproject's
# 300 s stays the default). The wall clock ends a run no test cap can, above a whole-suite run (~2 h on CI's 2 workers).
# macOS has no GNU timeout (coreutils' gtimeout stands in); without either the run has no wall clock.
wall=$(command -v timeout || command -v gtimeout || true)
[ -n "$wall" ] && wall="$wall --kill-after=60 ${AFFECTED_TESTS_WALL_SECONDS:-9000}"
rc=0
# shellcheck disable=SC2086 # $wall is empty or a command and its flags
OMNIGENT_SKIP_WEB_UI=true $wall \
  uv run --no-sync pytest -n auto --dist=loadfile --timeout="${AFFECTED_TESTS_TIMEOUT:-90}" "$@" || rc=$?
if [ -n "$record" ]; then
  python3 - "$work/cache/v/cache/lastfailed" "$record/failed-tests.json" <<'PY'
import json, os, sys
path = sys.argv[1]
failed = sorted(json.load(open(path))) if os.path.isfile(path) and os.path.getsize(path) else []
json.dump({"failed": failed}, open(sys.argv[2], "w"))
PY
fi
exit "$rc"
