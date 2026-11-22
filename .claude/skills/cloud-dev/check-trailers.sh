#!/bin/bash
# Fails when a commit the branch adds has a `Claude-Session:` line in its message.
# Added = reachable from HEAD but not from mine, the upstream remote's main, or the
# fork's mirror of it, so the upstream commits an upstream sync brings in pass.
# Usage: check-trailers.sh [head]   (default HEAD)
set -u
head=${1:-HEAD}
excludes=()
for ref in origin/mine upstream/main origin/main; do
  git rev-parse -q --verify "$ref^{commit}" >/dev/null && excludes+=("^$ref")
done
[ "${#excludes[@]}" -gt 0 ] || { echo "check-trailers: no origin/mine to compare with; fetch it first" >&2; exit 2; }
bad=()
while read -r sha; do
  git show -s --format=%B "$sha" | grep -qiE '^[[:space:]]*Claude-Session:' && bad+=("$sha")
done < <(git rev-list --no-merges "$head" "${excludes[@]}")
[ "${#bad[@]}" -eq 0 ] && exit 0
echo "Commits with a Claude-Session: line in their message:" >&2
for sha in "${bad[@]}"; do git log -1 --format='  %h %s' "$sha" >&2; done
echo "Reword each one to drop the line, then push again." >&2
exit 1
