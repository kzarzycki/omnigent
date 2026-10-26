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
# Warm uv's cache for the session's Gate sync, alongside the installs below. A setup
# over about 5 minutes isn't cached: a cold sync takes 393 s, from the image's partial
# cache 95 s, so 210 s covers the usual case and leaves 90 s spare.
warm_up_seconds=210
repo=""
for dir in /home/user/omnigent /home/user/*; do
  [ -f "$dir/uv.lock" ] && grep -q '^name = "omnigent"' "$dir/pyproject.toml" 2>/dev/null && { repo=$dir; break; }
done
warm_up() {
  [ -n "$repo" ] || { echo "uv warm-up skipped: no omnigent checkout under /home/user" >> "$log"; return; }
  local started=$SECONDS rc=0
  (cd "$repo" && OMNIGENT_SKIP_WEB_UI=true timeout -k 10 "$warm_up_seconds" \
    uv sync --locked --extra all --group test --group lint) >/dev/null 2>&1 || rc=$?
  case $rc in
    0) echo "uv warm-up done in $((SECONDS - started)) s" >> "$log" ;;
    124|137) echo "uv warm-up stopped after $warm_up_seconds s; the session's sync fetches the rest" >> "$log" ;;
    *) echo "uv warm-up FAILED with exit $rc; the session's sync fetches the rest" >> "$log" ;;
  esac
}
warm_up &
# bwrap: the linux_bwrap sandbox tests; ssh-keygen: SSH signature checks
{ $sudo apt-get update -qq && $sudo apt-get install -y -qq bubblewrap openssh-client >/dev/null; } \
  || echo "apt-get failed" >> "$log"
uv tool install -q pre-commit || echo "uv tool install pre-commit failed" >> "$log"
# from npm: the mise.run installer left no mise in the cloud image
command -v mise >/dev/null || npm install -g -s @jdxcode/mise || echo "npm install mise failed" >> "$log"
wait
missing=""
for tool in bwrap ssh-keygen pre-commit mise; do
  command -v "$tool" >/dev/null || missing="$missing $tool"
done
if [ -n "$missing" ]; then
  echo "omnigent-dev init FAILED, missing:$missing" >> "$log"
  exit 0
fi
echo "omnigent-dev init done: $(bwrap --version | head -1); $(mise --version | head -1)" >> "$log"
