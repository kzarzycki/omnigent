#!/bin/sh
# #695: each test gets its own user config home, so a config one test writes never
# reaches a later one, and the home stays inside the suite's data dir.
set -e
exec uv run --no-sync pytest -q -p no:cacheprovider -p no:randomly --reruns 0 \
  tests/test_config.py tests/cli/test_cli.py tests/inner/test_cursor_executor.py \
  tests/test_suite_isolation.py
