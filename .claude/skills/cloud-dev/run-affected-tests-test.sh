#!/usr/bin/env bash
# run-affected-tests.sh with a fake selector and a fake uv: pytest gets the per-test cap, and a hung run
# ends at the wall-clock limit with a nonzero exit.
set -u
here=$(cd "$(dirname "$0")" && pwd)
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
mkdir "$tmp/bin"
printf '#!/bin/bash\necho tests/example_test.py\n' > "$tmp/bin/python3"
printf '#!/bin/bash\nprintf "%%s\\n" "$@" > "$RECEIVED"\n[ -n "${HANG:-}" ] && exec sleep 30\n' > "$tmp/bin/uv"
chmod +x "$tmp/bin/python3" "$tmp/bin/uv"
RECEIVED="$tmp/args" PATH="$tmp/bin:$PATH" bash "$here/run-affected-tests.sh" >/dev/null 2>&1
grep -qx -- '--timeout=90' "$tmp/args" || { echo "!! affected tests: no per-test cap in: $(cat "$tmp/args")"; exit 1; }
RECEIVED="$tmp/args" HANG=1 AFFECTED_TESTS_WALL_SECONDS=1 PATH="$tmp/bin:$PATH" bash "$here/run-affected-tests.sh" >/dev/null 2>&1; rc=$?
[ "$rc" = 124 ] || { echo "!! affected tests: a hung run exited $rc, not 124"; exit 1; }
echo "cloud-dev run-affected-tests.sh: ok"
