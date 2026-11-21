#!/bin/sh
# Bypass rule of #521: a created ACP session may bypass approval only under a creator in bypass.
set -e
exec uv run --no-sync pytest -q -p no:cacheprovider --reruns 0 \
  tests/server/integration/test_acp_bypass_creator_modes.py \
  tests/server/integration/test_acp_model_selection.py -k "bypass or permission"
