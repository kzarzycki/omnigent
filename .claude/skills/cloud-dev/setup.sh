#!/bin/bash
# Setup script of the `omnigent-dev` Claude cloud environment: the tools Omnigent's
# tests and the fork's gate need that the cloud image lacks. The environment runs
# it at session start, so a change here takes effect only once applied (SKILL.md).
# A failed install is logged as FAILED with the missing tools, and the script still
# exits 0: a non-zero exit fails the whole cloud session, which the bridge never reports.
set -u
export PATH="$PATH:$HOME/.local/bin:/usr/local/bin"  # where uv tool and npm -g install
log=/tmp/omnigent-dev-init.log
sudo=""
[ "$(id -u)" = 0 ] || sudo=sudo
# bwrap: the linux_bwrap sandbox tests; ssh-keygen: SSH signature checks
{ $sudo apt-get update -qq && $sudo apt-get install -y -qq bubblewrap openssh-client >/dev/null; } \
  || echo "apt-get failed" >> "$log"
uv tool install -q pre-commit || echo "uv tool install pre-commit failed" >> "$log"
# from npm: the mise.run installer left no mise in the cloud image
command -v mise >/dev/null || npm install -g -s @jdxcode/mise || echo "npm install mise failed" >> "$log"
missing=""
for tool in bwrap ssh-keygen pre-commit mise; do
  command -v "$tool" >/dev/null || missing="$missing $tool"
done
if [ -n "$missing" ]; then
  echo "omnigent-dev init FAILED, missing:$missing" >> "$log"
  exit 0
fi
echo "omnigent-dev init done: $(bwrap --version | head -1); $(mise --version | head -1)" >> "$log"
