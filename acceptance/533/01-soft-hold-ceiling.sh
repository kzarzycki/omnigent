#!/usr/bin/env bash
# An idle runner stops honouring a stale soft hold after runner.idle_hold_max_s without work progress; hard holds, fresh progress, excluded sessions and attached clients keep it (issue 533).
set -euo pipefail
uv run --no-sync pytest -q \
  "tests/runner/test_runner_entry.py::test_inactivity_monitor_bounds_a_soft_hold_without_work_progress" \
  "tests/runner/test_runner_entry.py::test_inactivity_monitor_keeps_a_soft_hold_when_the_ceiling_is_zero" \
  "tests/runner/test_runner_entry.py::test_inactivity_monitor_logs_the_hold_once_and_the_reap" \
  "tests/runner/test_runner_entry.py::test_idle_hold_max_defaults_to_four_hours_and_zero_disables" \
  "tests/runner/test_runner_entry.py::test_exclude_list_keeps_a_listed_session_and_an_unreadable_list_keeps_all" \
  "tests/runner/test_runner_idle_active_work.py::test_soft_pane_holds_never_hide_a_human_wait" \
  "tests/runner/test_runner_idle_active_work.py::test_each_hard_live_work_signal_is_named" \
  "tests/runner/test_runner_idle_active_work.py::test_native_pane_with_live_work_holds_watchdog_despite_idle_edge"
