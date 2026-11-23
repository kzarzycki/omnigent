#!/usr/bin/env bash
# Run pytest on the tests the change touches (selector: affected_tests.py).
# Shared by `mise run check-fast` and the fork CI so both run the same line.
set -euo pipefail
cd "$(dirname "$0")/../../.."
targets=$(python3 .claude/skills/cloud-dev/affected_tests.py)
if [ -z "$targets" ]; then echo "affected tests: none touched"; exit 0; fi
echo "affected tests: $targets"
# Per-test cap: the slowest passing test took 38 s on a loaded 4-CPU VM, so 90 s ends a hang early (pyproject's
# 300 s stays the default). The wall clock ends a run no test cap can, above a whole-suite run (~2 h on CI's 2 workers).
# macOS has no GNU timeout (coreutils' gtimeout stands in); without either the run has no wall clock.
wall=$(command -v timeout || command -v gtimeout || true)
[ -n "$wall" ] && wall="$wall --kill-after=60 ${AFFECTED_TESTS_WALL_SECONDS:-9000}"
# shellcheck disable=SC2086 # one target per line, no spaces in paths; $wall is empty or a command and its flags
OMNIGENT_SKIP_WEB_UI=true $wall \
  uv run --no-sync pytest -n auto --dist=loadfile --timeout="${AFFECTED_TESTS_TIMEOUT:-90}" $targets
