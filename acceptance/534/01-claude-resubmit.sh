#!/usr/bin/env bash
# A Claude message the pane did not accept in time is resent after a wait, not dropped (issue 534).
set -euo pipefail
uv run --no-sync pytest -q \
  "tests/runner/test_claude_sign_in_hold.py::test_submit_claude_did_not_accept_is_resent_after_a_wait" \
  "tests/runner/test_claude_sign_in_hold.py::test_submit_refused_past_every_wait_fails_once_with_its_code" \
  "tests/inner/test_claude_native_executor.py::test_run_turn_preserves_refused_input_but_keeps_post_submit_failures_ambiguous" \
  "tests/harnesses/claude_native/test_claude_native_bridge.py::test_resend_of_an_unaccepted_submit_checks_whether_it_landed_late"
