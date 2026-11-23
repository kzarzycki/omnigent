#!/bin/sh
# #535: a running session held by an approval prompt reads awaiting_approval in
# sys_session_list and sys_session_get_info; once answered, or with no prompt, it is unchanged.
set -e
exec uv run --no-sync pytest -q -p no:cacheprovider --reruns 0 \
  tests/runner/test_runner_dispatch.py -k "awaiting_approval or get_info or session_list"
