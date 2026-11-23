#!/bin/sh
# Gaps a, d, e of #521: user agents, creator close, permission mode.
# Runs both whole test files so paging and the refused unrelated-target closes stay covered.
set -e
exec uv run --no-sync pytest -q -p no:cacheprovider --reruns 0 \
  tests/runner/test_runner_dispatch.py tests/runner/test_discovery_list_pagination.py
