#!/bin/bash
# check:all depends on check:setup, and check:setup (mise.toml) syncs Python always and
# installs the web packages whenever their node_modules are missing.
set -u
root=$(cd "$(dirname "$0")/../../.." && pwd)
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/bin" "$tmp/repo"
for tool in uv pnpm; do printf '#!/bin/bash\necho "%s $*" >> "%s/calls"\n' "$tool" "$tmp" > "$tmp/bin/$tool"; chmod +x "$tmp/bin/$tool"; done
cd "$tmp/repo" && git init -q && git config user.email t@t && git config user.name t && git config commit.gpgsign false
cp "$root/mise.toml" . && git add . && git commit -qm base && git update-ref refs/remotes/origin/mine HEAD
export PATH="$tmp/bin:$PATH" MISE_YES=1 MISE_TRUSTED_CONFIG_PATHS="$tmp/repo"
mise tasks deps check:all 2>&1 | grep -q 'check:setup' || { echo "!! check:all does not depend on check:setup"; exit 1; }
install='pnpm install --frozen-lockfile --filter web --filter omnigent-vscode'
probe() {  # probe <changed file> <node_modules present: none|web|all> <expected pnpm: 0|1>
  git reset -q --hard origin/mine; mkdir -p "$(dirname "$1")"; echo x > "$1"; git add . && git commit -qm probe
  rm -rf web/node_modules editors/vscode/node_modules "$tmp/calls"
  case "$2" in all) mkdir -p editors/vscode/node_modules web/node_modules ;; web) mkdir -p web/node_modules ;; esac
  mise run check:setup >/dev/null 2>&1 || { echo "!! $1: check:setup failed"; exit 1; }
  grep -q '^uv sync --locked --extra all --group test --group lint$' "$tmp/calls" || { echo "!! $1: no uv sync"; exit 1; }
  grep -qxF "$install" "$tmp/calls" && got=1 || got=0
  [ "$got" = "$3" ] || { echo "!! $1 (node_modules: $2): pnpm install ran=$got, expected $3; calls: $(cat "$tmp/calls")"; exit 1; }
}
# custom-lint's TypeScript check needs web/node_modules for any Python, shell or web change,
# so every change in a tree without them installs both projects.
for path in omnigent/a.py a.sh web/src/a.ts editors/vscode/src/a.ts docs/a.md; do probe "$path" none 1; done
probe web/src/a.ts web 1      # the VS Code packages are missing too
probe omnigent/a.py all 0     # installed: no install
echo "cloud-dev gate-setup: ok"
