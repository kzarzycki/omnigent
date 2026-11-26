#!/usr/bin/env bash
# Put each flake `approvals.py rerun-tests` named on its open `Flaky: <node id>` issue (a comment, or a new issue),
# and in the job summary. A flake is a "flake candidate" line of its output on a rerun it ended with its green line
# (the pack opens no issue itself). A flake whose issue call fails is written into this run's failed-tests.json, so
# the next run reruns it and files it again; this run then exits 1.
# Usage: record-flakes.sh <rerun-tests output> <this run's record directory>
# Env: GH_REPO, EARLIER_RUN, EARLIER_SHA, PR, HEAD_SHA, THIS_RUN; GITHUB_STEP_SUMMARY when set.
set -uo pipefail
output=$1 record=$2
summary=${GITHUB_STEP_SUMMARY:-/dev/null}
work=$(mktemp -d); trap 'rm -rf "$work"' EXIT
grep -q '^rerun-tests: green: ' "$output" 2>/dev/null || exit 0
sed -n 's/^rerun-tests: flake candidate: //p' "$output" | awk '!seen[$0]++' > "$work/flakes"
[ -s "$work/flakes" ] || exit 0
# Without the open issues a new one could duplicate one, so every flake waits for the next run instead.
listed=yes
gh api --paginate "repos/$GH_REPO/issues?state=open&per_page=100" \
  --jq '.[] | select(.pull_request | not) | [.number, .title] | @tsv' > "$work/open-issues" < /dev/null || listed=
echo "### Flaky: failed in $EARLIER_RUN, passed on this rerun" >> "$summary"
: > "$work/unfiled"
while IFS= read -r test; do
  title="Flaky: $test"
  body="Failed in $EARLIER_RUN (head $EARLIER_SHA) and passed on the rerun in $THIS_RUN (head $HEAD_SHA), for #$PR."
  number=$(TITLE="$title" awk -F'\t' '$2 == ENVIRON["TITLE"] || index($2, ENVIRON["TITLE"] " ") == 1 { print $1; exit }' "$work/open-issues")
  url=
  if [ -z "$listed" ]; then
    :
  elif [ -n "$number" ]; then
    url=$(gh api "repos/$GH_REPO/issues/$number/comments" -f body="$body" --jq .html_url < /dev/null) || url=
  else
    url=$(gh api "repos/$GH_REPO/issues" -f title="$title" -f body="$body" -f 'labels[]=bug' --jq .html_url < /dev/null) || url=
  fi
  if [ -n "$url" ]; then
    echo "Flaky: $test: $url"
    echo "- \`$test\`: $url" >> "$summary"
  else
    echo "!! Flaky: $test: not filed; recorded as failed so the next run reruns and files it"
    echo "- \`$test\`: not filed (the GitHub call failed); the next run reruns it" >> "$summary"
    echo "$test" >> "$work/unfiled"
  fi
done < "$work/flakes"
[ -s "$work/unfiled" ] || exit 0
python3 - "$record/failed-tests.json" "$work/unfiled" <<'PY'
import json, sys
path, unfiled = sys.argv[1], [t for t in open(sys.argv[2]).read().split("\n") if t]
try:
    failed = json.load(open(path))["failed"]
except (OSError, ValueError, KeyError):
    failed = []
json.dump({"failed": sorted(set(failed) | set(unfiled))}, open(path, "w"))
PY
exit 1
