#!/bin/sh
# claude-cloud usage reaches the session record and counters (issue 467).
exec uv run --no-sync pytest -q -p no:cacheprovider \
  tests/inner/test_claude_cloud_usage.py \
  tests/server/integration/test_sessions_endpoints.py -k "claude_cloud"
