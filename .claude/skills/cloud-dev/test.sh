#!/bin/bash
# setup.sh against fake installers: a failed install logs the missing tools as
# FAILED; with every tool present it logs done. Both exit 0 (setup.sh says why).
# The uv warm-up syncs with the Gate's flags from the repo root; a failed or
# timed-out warm-up is logged and still ends in done.
set -u
here=$(cd "$(dirname "$0")" && pwd)
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
run() {  # run <bin dir>: setup.sh with only that dir on PATH, so installed tools can't count
  sed -e "s#/tmp/omnigent-dev-init.log#$tmp/init.log#" -e '/^export PATH=/d' \
    -e "s#/home/user#$tmp/user#g" -e 's#^warm_up_seconds=.*#warm_up_seconds=1#' "$here/setup.sh" > "$tmp/setup.sh"
  for tool in id head timeout sleep grep; do ln -sf "$(command -v "$tool")" "$1/"; done
  rm -f "$tmp/init.log"
  HOME="$tmp/home" PATH="$1" "$BASH" "$tmp/setup.sh" >/dev/null 2>&1
}
fake() { printf '#!/bin/bash\n%s\n' "$2" > "$1"; chmod +x "$1"; }
mkdir -p "$tmp/fail" "$tmp/ok" "$tmp/home" "$tmp/user/omnigent"
printf '[project]\nname = "omnigent"\n' > "$tmp/user/omnigent/pyproject.toml"; touch "$tmp/user/omnigent/uv.lock"
for t in apt-get uv npm sudo; do fake "$tmp/fail/$t" "exit 42"; done
run "$tmp/fail"; rc=$?
[ "$rc" = 0 ] && grep -q "FAILED, missing: bwrap ssh-keygen pre-commit mise$" "$tmp/init.log" \
  || { echo "!! failed installs: exit $rc, log: $(cat "$tmp/init.log")"; exit 1; }
for t in apt-get npm sudo bwrap ssh-keygen pre-commit mise; do fake "$tmp/ok/$t" "echo $t 1.0"; done
check() {  # check <case> <sync step of the fake uv> <expected warm-up log line>
  fake "$tmp/ok/uv" "[ \"\$1\" = sync ] && { echo \"\$PWD \$OMNIGENT_SKIP_WEB_UI \$*\" > $tmp/sync.args; $2; }; echo uv 1.0"
  rm -f "$tmp/sync.args"
  run "$tmp/ok"; rc=$?
  [ "$rc" = 0 ] && grep -q "init done: bwrap 1.0; mise 1.0" "$tmp/init.log" && grep -q "$3" "$tmp/init.log" \
    && [ "$(cat "$tmp/sync.args" 2>/dev/null)" = "$tmp/user/omnigent true sync --locked --extra all --group test --group lint" ] \
    || { echo "!! $1: exit $rc, sync: $(cat "$tmp/sync.args" 2>/dev/null), log: $(cat "$tmp/init.log")"; exit 1; }
}
check "all tools present" "exit 0" "uv warm-up done in"
check "failed warm-up" "exit 3" "uv warm-up FAILED with exit 3"
check "timed-out warm-up" "exec sleep 5" "uv warm-up stopped after 1 s"
echo "cloud-dev setup.sh: ok"
# fork-env.sh: the env var beats the overlay, the overlay fills in, and neither fails naming the overlay
printf 'OMNIGENT_BRIDGE_HOST=from-overlay\n' > "$tmp/overlay.env"
fe() { env -u OMNIGENT_BRIDGE_HOST -u OMNIGENT_DEV_ENVIRONMENT_ID OMNIGENT_FORK_OVERLAY="$tmp/overlay.env" "$@"; }
[ "$(fe "$here/fork-env.sh" OMNIGENT_BRIDGE_HOST)" = from-overlay ] \
  && [ "$(fe OMNIGENT_BRIDGE_HOST=from-env "$here/fork-env.sh" OMNIGENT_BRIDGE_HOST)" = from-env ] \
  && ! fe "$here/fork-env.sh" OMNIGENT_DEV_ENVIRONMENT_ID 2>"$tmp/err" && grep -q "$tmp/overlay.env" "$tmp/err" \
  || { echo "!! fork-env.sh: $(cat "$tmp/err" 2>/dev/null)"; exit 1; }
echo "cloud-dev fork-env.sh: ok"
bash "$here/gate-setup-test.sh" || exit 1
# landing-setup.sh as landing-env.py sends it: installs the inlined guard as a managed
# PreToolUse hook, keeps other managed settings and adds it once on a rerun, with the
# checkout's guard replaced by `exit 0` or missing; unfilled, it logs FAILED.
export OMNIGENT_LAND_LOG="$tmp/land.log" OMNIGENT_LAND_MANAGED_DIR="$tmp/managed" OMNIGENT_LAND_NO_SUDO=1
checkout="$tmp/landuser/omnigent"
land() {  # land [script]: the setup script from the fake checkout, /home/user moved under $tmp
  [ $# = 1 ] || { python3 "$here/landing-env.py" --setup-script > "$tmp/landing-setup.sh" || return 9; }
  [ $# = 0 ] || cp "$1" "$tmp/landing-setup.sh"
  sed -i "s#/home/user#$tmp/landuser#g" "$tmp/landing-setup.sh"
  (cd "${checkout}" 2>/dev/null || cd "$tmp"; HOME="$tmp/landuser" bash "$tmp/landing-setup.sh" >/dev/null 2>&1)
}
landed() {  # landed <case>: one guard hook, other settings kept, the repo's guard refusing printenv
  hook=$(python3 -c 'import json,sys; s=json.load(open(sys.argv[1])); assert s["permissions"]["deny"] == ["WebFetch"]
entries = s["hooks"]["PreToolUse"]; assert len(entries) == 1 and entries[0]["matcher"] == "*", entries
print(entries[0]["hooks"][0]["command"])' "$tmp/managed/managed-settings.json") \
    && [ "$rc" = 0 ] && grep -q "omnigent-land init done" "$tmp/land.log" && ! grep -q FAILED "$tmp/land.log" \
    && cmp -s "$here/landing-guard.py" "$tmp/managed/landing-guard.py" \
    || { echo "!! landing-setup, $1: exit $rc, log: $(cat "$tmp/land.log")"; exit 1; }
  call='{"tool_name": "Bash", "tool_input": {"command": "printenv"}}'
  echo "$call" | OMNIGENT_LANDING_ENVIRONMENT=1 $hook 2>/dev/null; inside=$?
  echo "$call" | env -u OMNIGENT_LANDING_ENVIRONMENT $hook; outside=$?
  [ "$inside" = 2 ] && [ "$outside" = 0 ] \
    || { echo "!! landing-setup, $1: the installed hook did not refuse printenv only in the landing environment"; exit 1; }
}
fresh() { rm -rf "$tmp/managed" "$tmp/land.log"; mkdir -p "$tmp/managed"
  echo '{"permissions": {"deny": ["WebFetch"]}}' > "$tmp/managed/managed-settings.json"; }
mkdir -p "$checkout/.claude/skills/cloud-dev"
printf '#!/bin/sh\nexit 0\n' > "$checkout/.claude/skills/cloud-dev/landing-guard.py"
fresh; land && land; rc=$?; landed "checkout's guard is exit 0"
rm -rf "$tmp/landuser"
fresh; land; rc=$?; landed "no checkout"
fresh; land "$here/landing-setup.sh"; rc=$?
[ "$rc" = 0 ] && grep -q "omnigent-land init FAILED: the guard is not the one" "$tmp/land.log" \
  && ! grep -q "init done" "$tmp/land.log" \
  || { echo "!! landing-setup unfilled: exit $rc, log: $(cat "$tmp/land.log")"; exit 1; }
echo "cloud-dev landing-setup.sh: ok"
python3 "$here/landing-guard-test.py" -q 2>&1 | tail -1 | grep -qx OK \
  || { python3 "$here/landing-guard-test.py"; exit 1; }
echo "cloud-dev landing-guard.py: ok"
python3 "$here/landing-env.py" --selftest || exit 1
