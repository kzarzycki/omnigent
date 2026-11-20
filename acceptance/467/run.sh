#!/bin/sh
# claude-cloud usage reaches the session record, counters and session GET (issue 467).
exec uv run --no-sync pytest -q -p no:cacheprovider tests/inner/test_claude_cloud_usage.py \
  tests/server/routes/test_sessions_runner_relay.py -k "claude_cloud or cloud_usage"
