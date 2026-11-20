#!/bin/sh
# claude-cloud usage reaches the session record and counters (issue 467).
exec uv run --no-sync pytest -q -p no:cacheprovider \
  tests/inner/test_claude_cloud_usage.py tests/server/test_claude_cloud_usage_record.py
