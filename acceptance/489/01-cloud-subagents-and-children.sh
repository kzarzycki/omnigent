#!/bin/sh
# Spec of #489: a claude-cloud session's sub-agents show as its child sessions, its
# default child runs in the cloud, and a local child on the bridge host names its host.
set -e
uv run --no-sync pytest -q -p no:cacheprovider --reruns 0 \
  tests/inner/test_claude_cloud_subagents.py \
  tests/runner/test_acp_subagent_forwarding.py \
  tests/runner/test_cloud_children.py \
  tests/server/integration/test_cloud_child_workspace.py
uv run --no-sync pytest -q -p no:cacheprovider --reruns 0 \
  tests/server/integration/test_sessions_endpoints.py -k "acp_subagent"
(cd web && npx vitest run src/shell/subagentLabel.test.ts)
exec python3 integrations/claude-cloud-acp/claude_cloud_acp.py --self-check
