#!/usr/bin/env bash
# Run pytest on the tests the change touches (selector: affected_tests.py).
# Shared by `mise run check-fast` and the fork CI so both run the same line.
set -euo pipefail
cd "$(dirname "$0")/../../.."
targets=$(python3 .claude/skills/cloud-dev/affected_tests.py)
if [ -z "$targets" ]; then echo "affected tests: none touched"; exit 0; fi
echo "affected tests: $targets"
# Tests that fail on mine run as non-strict xfail (known_failures.txt), so a head run needs no base run.
export PYTHONPATH="$PWD/.claude/skills/cloud-dev${PYTHONPATH:+:$PYTHONPATH}"
export PYTEST_ADDOPTS="-p known_failures ${PYTEST_ADDOPTS:-}"
# shellcheck disable=SC2086 # one target per line, no spaces in paths
OMNIGENT_SKIP_WEB_UI=true uv run --no-sync pytest -n auto --dist=loadfile $targets
