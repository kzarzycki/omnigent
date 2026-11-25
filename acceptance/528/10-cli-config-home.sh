#!/bin/sh
# #528: the CLI and host tests pass together, whatever order they run in.
set -e
exec uv run --no-sync pytest -q -p no:cacheprovider --reruns 0 -n 4 \
  tests/cli/test_cli.py tests/host/test_connect.py
