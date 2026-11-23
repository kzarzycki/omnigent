#!/usr/bin/env bash
# Run pytest on the tests the change touches (selector: affected_tests.py).
# Shared by `mise run check-fast` and the fork CI so both run the same line.
# AFFECTED_TESTS_SINCE: an earlier run's record to build on (affected_tests.py --since).
# AFFECTED_TESTS_RECORD: a directory this run writes its record into, whatever pytest's outcome:
# tree (git ls-tree -r -z HEAD), selected, lastfailed.json, exit, and flakes (failed there, not
# reselected, passed here).
set -euo pipefail
cd "$(dirname "$0")/../../.."
selector=.claude/skills/cloud-dev/affected_tests.py
since=${AFFECTED_TESTS_SINCE:-}
record=${AFFECTED_TESTS_RECORD:-}
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
targets=$(python3 "$selector")
run=$targets
: > "$work/candidates"
if [ -n "$since" ]; then
  if run=$(python3 "$selector" --since "$since" 2> "$work/why"); then
    sed -n 's/^affected tests: flake candidate: //p' "$work/why" > "$work/candidates"
  else
    run=$targets
    echo "affected tests: the selector failed on the record; running the full selection" >> "$work/why"
  fi
  cat "$work/why" >&2
fi
if [ -n "$record" ]; then
  mkdir -p "$record"
  rm -f "$record/exit"
  git ls-tree -r -z HEAD > "$record/tree"
  printf '%s\n' "$targets" > "$record/selected"
  printf '{}\n' > "$record/lastfailed.json"
  : > "$record/flakes"
fi
if [ -z "$run" ]; then
  echo "affected tests: none touched"
  [ -z "$record" ] || echo 0 > "$record/exit"
  exit 0
fi
echo "affected tests: $run"
# One argument per line, so a node id with spaces stays whole; a fresh cache holds only this run's failures.
set --
[ -z "$since$record" ] || set -- -o "cache_dir=$work/cache"
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
lastfailed=$work/cache/v/cache/lastfailed
if [ -n "$record" ]; then
  [ ! -s "$lastfailed" ] || cp "$lastfailed" "$record/lastfailed.json"
  echo "$rc" > "$record/exit"
fi
# A flake: failed in the earlier run, reselected by nothing, and not failed in this complete run.
if [ -s "$work/candidates" ] && { [ "$rc" = 0 ] || [ "$rc" = 1 ]; }; then
  if [ -s "$lastfailed" ]; then
    python3 -c 'import json, sys; print("\n".join(json.load(open(sys.argv[1]))))' "$lastfailed" > "$work/failed"
  else
    : > "$work/failed"
  fi
  grep -vxFf "$work/failed" "$work/candidates" > "$work/flakes" || true
  sed 's/^/affected tests: flaky, passed on the rerun: /' "$work/flakes" >&2
  [ -z "$record" ] || cp "$work/flakes" "$record/flakes"
fi
exit "$rc"
