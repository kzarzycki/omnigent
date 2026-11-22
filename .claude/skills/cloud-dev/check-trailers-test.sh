#!/bin/bash
# check-trailers.sh: a fork commit with the trailer fails, without it passes,
# an upstream commit with it passes (as an upstream sync brings in).
set -u
here=$(cd "$(dirname "$0")" && pwd)
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
cd "$tmp" && git init -q -b main . && git config user.email t@example.com && git config user.name t
git commit -q --allow-empty -m base
git branch mine
git commit -q --allow-empty -m "upstream change" -m "Claude-Session: https://example.com/s/1"
git update-ref refs/remotes/origin/main main
git update-ref refs/remotes/origin/mine mine
git checkout -q -b pr mine
git commit -q --allow-empty -m "clean fork change"
bash "$here/check-trailers.sh" 2>/dev/null || { echo "!! clean fork commit failed"; exit 1; }
git merge -q --no-edit main                      # sync: upstream commit with the trailer comes in
bash "$here/check-trailers.sh" 2>/dev/null || { echo "!! upstream commit with trailer failed"; exit 1; }
git commit -q --allow-empty -m "fork change" -m "Claude-Session: https://example.com/s/2"
out=$(bash "$here/check-trailers.sh" 2>&1); rc=$?
[ "$rc" = 1 ] && grep -q "fork change" <<<"$out" && grep -q "push again" <<<"$out" \
  || { echo "!! fork commit with trailer: exit $rc: $out"; exit 1; }
echo "cloud-dev check-trailers.sh: ok"
