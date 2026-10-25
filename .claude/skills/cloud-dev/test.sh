#!/bin/bash
# setup.sh against fake installers: a failed install logs the missing tools as
# FAILED; with every tool present it logs done. Both exit 0 (setup.sh says why).
set -u
here=$(cd "$(dirname "$0")" && pwd)
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
run() {  # run <bin dir>: setup.sh with only that dir on PATH, so installed tools can't count
  sed -e "s#/tmp/omnigent-dev-init.log#$tmp/init.log#" -e '/^export PATH=/d' "$here/setup.sh" > "$tmp/setup.sh"
  ln -sf "$(command -v id)" "$(command -v head)" "$1/"
  rm -f "$tmp/init.log"
  HOME="$tmp/home" PATH="$1" "$BASH" "$tmp/setup.sh" >/dev/null 2>&1
}
fake() { printf '#!/bin/bash\n%s\n' "$2" > "$1"; chmod +x "$1"; }
mkdir -p "$tmp/fail" "$tmp/ok" "$tmp/home"
for t in apt-get uv npm sudo; do fake "$tmp/fail/$t" "exit 42"; done
run "$tmp/fail"; rc=$?
[ "$rc" = 0 ] && grep -q "FAILED, missing: bwrap ssh-keygen pre-commit mise$" "$tmp/init.log" \
  || { echo "!! failed installs: exit $rc, log: $(cat "$tmp/init.log")"; exit 1; }
for t in apt-get uv npm sudo bwrap ssh-keygen pre-commit mise; do fake "$tmp/ok/$t" "echo $t 1.0"; done
run "$tmp/ok"; rc=$?
[ "$rc" = 0 ] && grep -q "init done: bwrap 1.0; mise 1.0" "$tmp/init.log" \
  || { echo "!! all tools present: exit $rc, log: $(cat "$tmp/init.log")"; exit 1; }
echo "cloud-dev setup.sh: ok"
# fork-env.sh: the env var beats the overlay, the overlay fills in, and neither fails naming the overlay
printf 'OMNIGENT_BRIDGE_HOST=from-overlay\n' > "$tmp/overlay.env"
fe() { env -u OMNIGENT_BRIDGE_HOST -u OMNIGENT_DEV_ENVIRONMENT_ID OMNIGENT_FORK_OVERLAY="$tmp/overlay.env" "$@"; }
[ "$(fe "$here/fork-env.sh" OMNIGENT_BRIDGE_HOST)" = from-overlay ] \
  && [ "$(fe OMNIGENT_BRIDGE_HOST=from-env "$here/fork-env.sh" OMNIGENT_BRIDGE_HOST)" = from-env ] \
  && ! fe "$here/fork-env.sh" OMNIGENT_DEV_ENVIRONMENT_ID 2>"$tmp/err" && grep -q "$tmp/overlay.env" "$tmp/err" \
  || { echo "!! fork-env.sh: $(cat "$tmp/err" 2>/dev/null)"; exit 1; }
echo "cloud-dev fork-env.sh: ok"
