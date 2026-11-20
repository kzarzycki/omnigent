#!/usr/bin/env bash
# Run pytest on the tests the change touches (selector: affected_tests.py).
# Shared by `mise run check-fast` and the fork CI so both run the same line.
set -euo pipefail
cd "$(dirname "$0")/../../.."
targets=$(python3 .claude/skills/cloud-dev/affected_tests.py)
if [ -z "$targets" ]; then echo "affected tests: none touched"; exit 0; fi
echo "affected tests: $targets"
# shellcheck disable=SC2086 # one target per line, no spaces in paths
OMNIGENT_SKIP_WEB_UI=true uv run --no-sync pytest -n auto --dist=loadfile $targets
