#!/bin/sh
# Gap h of #521: a session launched outside a tool call records its launcher, which must own it.
set -e
exec uv run --no-sync pytest -q -p no:cacheprovider --reruns 0 \
  tests/server/integration/test_session_orchestration.py tests/server/integration/test_acp_model_selection.py \
  -k "launcher"
