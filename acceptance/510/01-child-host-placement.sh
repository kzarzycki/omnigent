#!/usr/bin/env bash
# A child naming another host is placed and checked on that host; without a host it inherits the parent's runner (issue 510).
set -euo pipefail
uv run --no-sync pytest -q \
  tests/server/integration/test_session_create_harness_readiness.py \
  "tests/server/integration/test_session_host_launch.py::test_child_naming_another_ready_host_launches_there_not_on_parent_runner"
