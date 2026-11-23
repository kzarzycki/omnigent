#!/bin/sh
# Gap b of #521: a claude-cloud child can be created in a chosen Claude cloud environment.
set -e
uv run --no-sync pytest -q -p no:cacheprovider --reruns 0 \
  tests/server/integration/test_acp_model_selection.py tests/tools/builtins/test_session_create_schema_placement.py \
  tests/runner/test_runner_dispatch.py -k "cloud_environment"
exec python3 integrations/claude-cloud-acp/claude_cloud_acp.py --self-check
