#!/bin/bash
# Setup script of the landing-only Claude cloud environment (landing-env.py). It installs
# no packages: it puts landing-guard.py in Claude Code's managed settings as a PreToolUse
# hook, which a checked-out branch can't edit or drop. Log: /tmp/omnigent-land-init.log;
# a landing session that finds FAILED there lands nothing. Exits 0 either way, since a
# non-zero exit fails the whole cloud session, which the bridge never reports.
set -u
log=/tmp/omnigent-land-init.log
managed=/etc/claude-code
sudo=""
[ "$(id -u)" = 0 ] || sudo=sudo
guard=""
for dir in /home/user/omnigent /home/user/*; do
  [ -f "$dir/.claude/skills/cloud-dev/landing-guard.py" ] && { guard="$dir/.claude/skills/cloud-dev/landing-guard.py"; break; }
done
[ -n "$guard" ] || { echo "omnigent-land init FAILED: no landing-guard.py under /home/user" >> "$log"; exit 0; }
$sudo mkdir -p "$managed" && $sudo install -m 0644 "$guard" "$managed/landing-guard.py" \
  && $sudo python3 - "$managed/managed-settings.json" "python3 $managed/landing-guard.py" <<'EOF' \
  || { echo "omnigent-land init FAILED: the guard hook was not installed" >> "$log"; exit 0; }
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
