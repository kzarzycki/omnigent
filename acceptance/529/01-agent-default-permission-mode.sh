#!/bin/sh
# Agent default permission mode: applied on each start path, caps requests, replaces #526/#520 special cases (issue 529).
set -e
uv run --no-sync pytest -q -p no:cacheprovider --reruns 0 \
  tests/native/test_permission_modes.py \
  tests/server/integration/test_acp_default_permission_mode.py \
  tests/server/integration/test_acp_model_selection.py -k "permission or bypass or default"
uv run --no-sync pytest -q -p no:cacheprovider --reruns 0 \
  tests/server/integration/test_session_orchestration.py -k "permission_mode or agent_default or launch_args_are_held or for_agents_only or no_declared_default"
exec uv run --no-sync pytest -q -p no:cacheprovider --reruns 0 \
  tests/server/integration/test_sessions_endpoints.py -k "codex_native_undeclared or undeclared_codex_spec or resumed_codex_session or declared_native_default or declared_native_mode or harness_override_cannot or keeps_an_explicit_default or named_cursor_child or codex_spec_opt_out or codex_explicit_stricter"
