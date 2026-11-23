#!/usr/bin/env bash
# A Claude message the pane did not accept in time is resent after a wait, not dropped (issue 534).
set -euo pipefail
hold=tests/runner/test_claude_sign_in_hold.py
bridge=tests/harnesses/claude_native/test_claude_native_bridge.py
uv run --no-sync pytest -q \
  "$hold::test_submit_claude_did_not_accept_is_resent_after_a_wait" \
  "$hold::test_submit_refused_past_every_wait_fails_once_with_its_code" \
  "$hold::test_message_the_server_queued_longer_than_the_wait_fails_at_once" \
  "$hold::test_refusal_without_undelivered_evidence_fails_as_before" \
  "$hold::test_interrupt_or_stop_during_the_hold_drops_the_held_message" \
  "$hold::test_interrupt_or_stop_before_the_refused_stream_ends_is_never_undone" \
  "$hold::test_child_agent_dispatch_refused_by_a_sign_in_fails_at_once" \
  "$hold::test_refusal_on_a_stream_a_newer_turn_took_over_is_reported_not_held" \
  "tests/inner/test_claude_native_executor.py::test_run_turn_preserves_refused_input_but_keeps_post_submit_failures_ambiguous" \
  "$bridge::test_a_refused_submit_is_remembered_for_the_resend_of_the_same_text" \
  "$bridge::test_resend_of_an_unaccepted_submit_checks_whether_it_landed_late" \
  "$bridge::test_original_that_lands_during_the_resend_is_not_typed_again" \
  "$bridge::test_resend_into_a_pane_still_stuck_on_the_draft_stays_not_accepted" \
  "$bridge::test_resend_types_again_when_the_capture_gives_no_sign_the_original_was_submitted" \
  "$bridge::test_an_echo_already_on_screen_before_the_submit_is_no_sign_it_landed"
