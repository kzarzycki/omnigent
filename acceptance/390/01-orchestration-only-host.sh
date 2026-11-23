#!/bin/sh
# A host marked orchestration-only refuses a local work session through the session create
# route (the REST API and the MCP session tools share it), allows listed and cloud agents,
# and leaves an unmarked host unchanged.
set -e
exec uv run --no-sync pytest -q -p no:cacheprovider --reruns 0 \
  tests/server/integration/test_orchestration_hosts.py
