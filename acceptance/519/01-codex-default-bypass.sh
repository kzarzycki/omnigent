#!/bin/sh
# A codex-native session with no declared stance launches in full bypass; a stricter one wins (issue 519).
exec uv run --no-sync pytest -q -p no:cacheprovider \
  tests/server/routes/test_sessions_yolo_launch_args.py \
  tests/server/integration/test_sessions_endpoints.py \
  tests/server/integration/test_sessions_harness_override.py \
  tests/server/integration/test_session_orchestration.py \
  -k "codex_native_undeclared or codex_native_default_args or codex_native_yolo_false or codex_native_permission_mode or codex_native_unsupported or undeclared_codex_spec or named_child_overridden_to_codex or unloadable_spec or harness_override_without_stance or explicit_stricter_stance or claude_keys_follow or scheduled_origin_workers or opt_out_keeps_launch_args_unset"
