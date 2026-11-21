#!/bin/sh
# Gaps a, c, d, e of #521: user agents, creator filter and cursor, creator close, permission mode.
set -e
exec uv run --no-sync pytest -q -p no:cacheprovider --reruns 0 \
  tests/runner/test_runner_dispatch.py tests/runner/test_discovery_list_pagination.py \
  -k "user_agents or merges_three_sources or creator or permission_mode or get_info or list_filters"
