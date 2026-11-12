#!/bin/bash
# Setup script of the landing-only Claude cloud environment, as landing-env.py sends it:
# it fills the line below with landing-guard.py, so the guard comes from the environment
# and nothing here reads the session's checkout. It installs no packages: it puts the
# guard in Claude Code's managed settings as a PreToolUse hook, which a checked-out branch
# can't edit or drop. Log: /tmp/omnigent-land-init.log; a landing session that finds
# FAILED there lands nothing. Exits 0 either way, since a non-zero exit fails the whole
# cloud session, which the bridge never reports.
# OMNIGENT_LAND_MANAGED_DIR, OMNIGENT_LAND_LOG and OMNIGENT_LAND_NO_SUDO=1 are for tests.
set -u
log=${OMNIGENT_LAND_LOG:-/tmp/omnigent-land-init.log}
managed=${OMNIGENT_LAND_MANAGED_DIR:-/etc/claude-code}
sudo=""
[ "$(id -u)" = 0 ] || [ "${OMNIGENT_LAND_NO_SUDO:-}" = 1 ] || sudo=sudo
fail() { echo "omnigent-land init FAILED: $1" >> "$log"; exit 0; }
$sudo mkdir -p "$managed" || fail "no managed settings directory"
$sudo tee "$managed/landing-guard.py" > /dev/null << 'LANDING_GUARD_EOF' || fail "the guard was not written"
@LANDING_GUARD_PY@
LANDING_GUARD_EOF
# An unfilled template leaves a line that isn't Python, so it fails here.
$sudo chmod 0644 "$managed/landing-guard.py" \
  && python3 -c 'import ast, sys; ast.parse(open(sys.argv[1]).read())' "$managed/landing-guard.py" \
  || fail "the guard is not the one landing-env.py inlines"
$sudo python3 - "$managed/managed-settings.json" "python3 $managed/landing-guard.py" << 'EOF' \
  || fail "the guard hook was not installed"
import json, os, sys
path, command = sys.argv[1:]
settings = json.load(open(path)) if os.path.exists(path) else {}
entries = settings.setdefault("hooks", {}).setdefault("PreToolUse", [])
hook = {"matcher": "*", "hooks": [{"type": "command", "command": command}]}
if hook not in entries:
    entries.append(hook)
with open(path, "w") as out:
    json.dump(settings, out, indent=2)
EOF
echo "omnigent-land init done: guard hook in $managed/managed-settings.json" >> "$log"
