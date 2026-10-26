#!/bin/bash
# Print one of the owner's machine-specific values, which stay out of this public repo:
#   fork-env.sh OMNIGENT_BRIDGE_HOST | OMNIGENT_MACOS_BUILD_HOST | OMNIGENT_DEV_ENVIRONMENT_ID
#             | OMNIGENT_AGENT_SKILLS (the bridge host's agent-skills checkout)
# The environment variable wins (a Claude cloud environment sets it), else the overlay's
# NAME=value line. Missing from both, it exits 1 naming the overlay.
set -u
name=${1:?usage: fork-env.sh NAME}
overlay="${OMNIGENT_FORK_OVERLAY:-$HOME/.config/omnigent/fork-dev.conf}"
value=${!name:-}
[ -n "$value" ] || value=$(sed -n "s/^$name=//p" "$overlay" 2>/dev/null | tail -1)
[ -n "$value" ] || { echo "fork-env.sh: $name is not set: export it, or add $name=... to $overlay" >&2; exit 1; }
echo "$value"
