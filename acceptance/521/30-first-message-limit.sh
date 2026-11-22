#!/bin/sh
# Gap f of #521: a 19,000-character worker brief is accepted; an oversize one is refused naming the limit.
set -e
exec uv run --no-sync pytest -q -p no:cacheprovider --reruns 0 \
  tests/runner/test_session_orchestration.py tests/server/integration/test_session_orchestration.py \
  -k "long_worker_brief or long_brief or message_schema or initial_message_is_validated or oversize"
