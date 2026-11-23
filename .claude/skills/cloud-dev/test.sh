#!/bin/bash
# setup.sh against fake installers: a failed install logs the missing tools as
# FAILED; with every tool present it logs done. Both exit 0 (setup.sh says why).
# The uv step replaces a uv older than uv.toml requires, or logs FAILED and goes on.
# The uv warm-up syncs with the Gate's flags from the repo root; a failed or
# timed-out warm-up is logged and still ends in done. A gh-quota shim that fails to
# fetch or its checksum is logged, installs nothing, and still ends in done.
set -u
here=$(cd "$(dirname "$0")" && pwd)
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
# gatekeeper.md as approvals.py reads it. pick_approvals prints the script to run: APPROVALS_PY as given,
# else the pinned pack's gatekeeper approvals.py (OMNIGENT_ENGINEERING_PACK, so the gate moves only when the pin
# does; a pin without the file fails, never skips), else the moving agent-skills checkout's (empty when unset).
pick_approvals() {
  [ -n "${APPROVALS_PY:-}" ] && { echo "$APPROVALS_PY"; return; }
  local pack; pack=$("$here/fork-env.sh" OMNIGENT_ENGINEERING_PACK 2>/dev/null)
  if [ -n "$pack" ]; then
    [ -f "$pack/gatekeeper/skills/gatekeeper/scripts/approvals.py" ] || { echo "OMNIGENT_ENGINEERING_PACK has no approvals.py: $pack/gatekeeper/skills/gatekeeper/scripts/approvals.py" >&2; return 1; }
    echo "$pack/gatekeeper/skills/gatekeeper/scripts/approvals.py"; return
  fi
  local skills; skills=$("$here/fork-env.sh" OMNIGENT_AGENT_SKILLS 2>/dev/null)
  [ -n "$skills" ] && echo "$skills/gatekeeper/skills/gatekeeper/scripts/approvals.py"
  return 0
}
# run_policy: the gate's policy step (POLICY_CMD swaps the policy test, for the check below). It runs "${policy_cmd[@]}" on the picked approvals.py, and fails when the
# pick fails (a stale pin) or the policy test does; with no approvals.py it logs a skip.
if [ -n "${POLICY_CMD:-}" ]; then read -ra policy_cmd <<<"$POLICY_CMD"
else policy_cmd=(uv run --no-sync python "$here/gatekeeper-policy-test.py"); fi
run_policy() {
  local approvals; approvals=$(pick_approvals) || return 1
  if [ -f "$approvals" ]; then
    "${policy_cmd[@]}" "$approvals" >"$tmp/policy.log" 2>&1 || { cat "$tmp/policy.log"; return 1; }
    tail -1 "$tmp/policy.log"
  else
    echo "gatekeeper policy: skipped, no approvals.py (set APPROVALS_PY or OMNIGENT_AGENT_SKILLS)"
  fi
}
if [ -z "${POLICY_ONLY:-}" ]; then
run() {  # run <bin dir>: setup.sh with only that dir on PATH, so installed tools can't count
  sed -e "s#/tmp/omnigent-dev-init.log#$tmp/init.log#" -e '/^export PATH=/d' \
    -e "s#/home/user#$tmp/user#g" -e 's#^warm_up_seconds=.*#warm_up_seconds=1#' "$here/setup.sh" > "$tmp/setup.sh"
  for tool in id head sleep grep sed sort cat mv sh mkdir cp touch mktemp sha256sum; do ln -sf "$(command -v "$tool")" "$1/"; done
  if command -v timeout >/dev/null; then ln -sf "$(command -v timeout)" "$1/"; else stand_in_timeout "$1/timeout"; fi
  rm -f "$tmp/init.log"
  HOME="$tmp/home" PATH="$1" "$BASH" "$tmp/setup.sh" >/dev/null 2>&1
}
# macOS has no GNU timeout: a stand-in for `timeout [-k N] SECS CMD...` that exits 124 on expiry.
stand_in_timeout() {
  cat > "$1" <<'PERL'
#!/usr/bin/perl
if ($ARGV[0] eq '-k') { shift; shift }
my $secs = shift;
my $pid = fork;
if (!$pid) { exec @ARGV or exit 127 }
$SIG{ALRM} = sub { kill 'KILL', $pid; waitpid $pid, 0; exit 124 };
alarm $secs; waitpid $pid, 0; exit($? >> 8);
PERL
  chmod +x "$1"
}
fake() { printf '#!/bin/bash\n%s\n' "$2" > "$1"; chmod +x "$1"; }
mkdir -p "$tmp/fail" "$tmp/ok" "$tmp/home" "$tmp/user/omnigent"
printf '[project]\nname = "omnigent"\n' > "$tmp/user/omnigent/pyproject.toml"; touch "$tmp/user/omnigent/uv.lock"
printf 'required-version = ">=0.13.4"\n' > "$tmp/user/omnigent/uv.toml"
for t in apt-get uv npm sudo; do fake "$tmp/fail/$t" "exit 42"; done
run "$tmp/fail"; rc=$?
[ "$rc" = 0 ] && grep -q "FAILED, missing: uv bwrap ssh-keygen pre-commit mise$" "$tmp/init.log" \
  && grep -q "gh-quota shim install failed" "$tmp/init.log" \
  || { echo "!! failed installs: exit $rc, log: $(cat "$tmp/init.log")"; exit 1; }
for t in apt-get npm sudo bwrap ssh-keygen pre-commit mise; do fake "$tmp/ok/$t" "echo $t 1.0"; done
# curl fetches files whose sums don't match the pinned ones: the shim step must refuse them
fake "$tmp/ok/curl" 'while [ $# -gt 0 ]; do [ "$1" = -o ] && { echo tampered > "$2"; shift; }; shift; done'
# fake_uv <path> <version> <tag> <sync step>: logs "<tag> <args>" per call, records sync's context
fake_uv() {
  fake "$1" "echo \"$3 \$*\" >> $tmp/uv.calls; case \$1 in --version) echo \"uv $2 (fake)\";; sync) echo \"\$PWD \$OMNIGENT_SKIP_WEB_UI \$*\" > $tmp/sync.args; $4;; esac"
}
fake "$tmp/ok/python3" "touch $tmp/pip.ran; . $tmp/installer.sh"
fake_uv "$tmp/newuv" 0.13.8 new "exit 0"
printf 'mkdir -p "$HOME/.local/bin" && cp %s "$HOME/.local/bin/uv"\n' "$tmp/newuv" > "$tmp/installer.sh"
check() {  # check <case> <sync step of the fake uv> <expected warm-up log line> [old uv version]
  fake_uv "$tmp/ok/uv" "${4:-0.13.8}" old "$2"
  rm -rf "$tmp/sync.args" "$tmp/uv.calls" "$tmp/pip.ran" "$tmp/home/.local" "$tmp/home/.bashrc"
  printf '[ -z "$PS1" ] && return\n' > "$tmp/home/.bashrc"
  run "$tmp/ok"; rc=$?
  [ "$rc" = 0 ] && grep -q "init done: uv 0.13.8; bwrap 1.0; mise 1.0" "$tmp/init.log" && grep -q "$3" "$tmp/init.log" \
    && [ "$(cat "$tmp/sync.args" 2>/dev/null)" = "$tmp/user/omnigent true sync --locked --extra all --group test --group lint" ] \
    || { echo "!! $1: exit $rc, sync: $(cat "$tmp/sync.args" 2>/dev/null), log: $(cat "$tmp/init.log")"; exit 1; }
}
check "satisfying uv" "exit 0" "uv warm-up done in"
grep -q "gh-quota shim install failed" "$tmp/init.log" && [ ! -e "$tmp/home/.local/share/gh-quota" ] \
  || { echo "!! gh-quota: a checksum mismatch installed or went unlogged: $(cat "$tmp/init.log")"; exit 1; }
[ ! -e "$tmp/pip.ran" ] && ! grep -q '^new' "$tmp/uv.calls" && ! grep -q omnigent-dev "$tmp/home/.bashrc" \
  || { echo "!! satisfying uv was replaced"; exit 1; }
check "failed warm-up" "exit 3" "uv warm-up FAILED with exit 3"
check "timed-out warm-up" "exec sleep 5" "uv warm-up stopped after 1 s"
# an old uv: replaced, every later uv call is the new one, and shells get it first on PATH
check "old uv" "exit 0" "uv warm-up done in" 0.11.32
[ -e "$tmp/pip.ran" ] && ! grep -v -- '--version' "$tmp/uv.calls" | grep -q '^old' && grep -q '^new sync' "$tmp/uv.calls" \
  && grep -q '^new tool install' "$tmp/uv.calls" && [ "$(head -1 "$tmp/home/.bashrc")" = 'export PATH="$HOME/.local/bin:$PATH"  # omnigent-dev: uv for uv.toml' ] \
  || { echo "!! old uv: calls: $(cat "$tmp/uv.calls"), bashrc: $(cat "$tmp/home/.bashrc")"; exit 1; }
# an old uv whose replacement fails: logged FAILED naming uv, still exit 0
rm -rf "$tmp/home/.local" "$tmp/init.log"; fake_uv "$tmp/ok/uv" 0.11.32 old "exit 0"; fake "$tmp/ok/python3" "exit 22"
run "$tmp/ok"; rc=$?
[ "$rc" = 0 ] && grep -q "uv install failed" "$tmp/init.log" && grep -q "FAILED, missing: uv$" "$tmp/init.log" \
  || { echo "!! failed uv install: exit $rc, log: $(cat "$tmp/init.log")"; exit 1; }
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
python3 "$here/affected-tests-test.py" || exit 1
bash "$here/run-affected-tests-test.sh" || exit 1
# the approvals.py the policy test receives through the gate's own last line (POLICY_ONLY=1 skips the rest of
# this script): pin over checkout, checkout when no pin, override over both, a stale pin exits nonzero
printf 'echo "$1" > "$RECEIVED"\n' > "$tmp/stub.sh"
got() {  # got <env assignments>: this script's policy step with a stub policy test; prints what the stub received
  rm -f "$tmp/received"
  env -u APPROVALS_PY -u OMNIGENT_ENGINEERING_PACK -u OMNIGENT_AGENT_SKILLS OMNIGENT_FORK_OVERLAY=/dev/null \
    POLICY_ONLY=1 POLICY_CMD="bash $tmp/stub.sh" RECEIVED="$tmp/received" "$@" bash "$here/test.sh" 2>&1 \
    || { echo "exit $?"; return 0; }
  cat "$tmp/received" 2>/dev/null
}
mkdir -p "$tmp/pin/gatekeeper/skills/gatekeeper/scripts" "$tmp/skills/gatekeeper/skills/gatekeeper/scripts"
checkout="$tmp/skills/gatekeeper/skills/gatekeeper/scripts/approvals.py"
pinned="$tmp/pin/gatekeeper/skills/gatekeeper/scripts/approvals.py"
touch "$pinned" "$checkout" "$tmp/override.py"
[ "$(got OMNIGENT_ENGINEERING_PACK="$tmp/pin" OMNIGENT_AGENT_SKILLS="$tmp/skills")" = "$pinned" ] \
  && [ "$(got OMNIGENT_AGENT_SKILLS="$tmp/skills")" = "$checkout" ] \
  && [ "$(got APPROVALS_PY="$tmp/override.py" OMNIGENT_ENGINEERING_PACK="$tmp/pin")" = "$tmp/override.py" ] \
  && [ "$(got OMNIGENT_ENGINEERING_PACK="$tmp/none" OMNIGENT_AGENT_SKILLS="$tmp/skills")" = \
       "OMNIGENT_ENGINEERING_PACK has no approvals.py: $tmp/none/gatekeeper/skills/gatekeeper/scripts/approvals.py
exit 1" ] \
  || { echo "!! approvals.py selection through the gate's policy step"; exit 1; }
# the default command keeps a script path with a space as one argument: a fake uv records its arguments
spaced="$tmp/work tree/.claude/skills/cloud-dev"; mkdir -p "$spaced" "$tmp/fakebin"
cp "$here/test.sh" "$here/fork-env.sh" "$spaced/"
printf '#!/bin/bash\nprintf "%%s\\n" "$@" > "$RECEIVED"\n' > "$tmp/fakebin/uv"; chmod +x "$tmp/fakebin/uv"
rm -f "$tmp/received"
env -u OMNIGENT_ENGINEERING_PACK -u OMNIGENT_AGENT_SKILLS OMNIGENT_FORK_OVERLAY=/dev/null POLICY_ONLY=1 \
  APPROVALS_PY="$tmp/override.py" PATH="$tmp/fakebin:$PATH" RECEIVED="$tmp/received" bash "$spaced/test.sh" >/dev/null 2>&1
[ "$(cat "$tmp/received" 2>/dev/null)" = "run
--no-sync
python
$spaced/gatekeeper-policy-test.py
$tmp/override.py" ] || { echo "!! default policy command splits a path with a space"; exit 1; }
echo "cloud-dev approvals.py selection: ok"
fi
run_policy || exit 1
