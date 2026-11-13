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
# uv: the image's is older than uv.toml's required-version, so every uv command refuses to
# run. Replace it first, with ~/.local/bin ahead on PATH here and in the session's shells.
uv_install_version=0.13.8
uv_min=0.13.4
[ -n "$repo" ] && uv_min=$(sed -n 's/^required-version *= *">=\([0-9][0-9.]*\)".*/\1/p' "$repo/uv.toml" 2>/dev/null | head -1)
[ -n "$uv_min" ] || uv_min=0.13.4
uv_version() { uv --version 2>/dev/null | sed -n 's/^uv \([0-9][0-9.]*\).*/\1/p'; }
uv_ok() {
  local have; have=$(uv_version)
  [ -n "$have" ] && [ "$(printf '%s\n%s\n' "$uv_min" "$have" | sort -V | head -1)" = "$uv_min" ]
}
if ! uv_ok; then
  export PATH="$HOME/.local/bin:$PATH"
  # from PyPI: the environment's network allows it, and blocks astral.sh's installer
  { python3 -m pip install -q --user "uv==$uv_install_version" \
    || python3 -m pip install -q --user --break-system-packages "uv==$uv_install_version"; } >/dev/null 2>&1 \
    || echo "uv install failed" >> "$log"
  hash -r
  if uv_ok; then  # ahead of .bashrc's non-interactive early return, so session shells find it
    path_line='export PATH="$HOME/.local/bin:$PATH"  # omnigent-dev: uv for uv.toml'
    grep -qF "$path_line" "$HOME/.bashrc" 2>/dev/null || {
      { printf '%s\n' "$path_line"; cat "$HOME/.bashrc" 2>/dev/null; } > "$HOME/.bashrc.new" \
        && mv "$HOME/.bashrc.new" "$HOME/.bashrc" || echo "bashrc PATH update failed" >> "$log"
    }
  fi
fi
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
uv_ok || missing=" uv"
for tool in bwrap ssh-keygen pre-commit mise; do
  command -v "$tool" >/dev/null || missing="$missing $tool"
done
if [ -n "$missing" ]; then
  echo "omnigent-dev init FAILED, missing:$missing" >> "$log"
  exit 0
fi
echo "omnigent-dev init done: uv $(uv_version); $(bwrap --version | head -1); $(mise --version | head -1)" >> "$log"
