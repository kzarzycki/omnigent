#!/usr/bin/env bash
# Put each flake of a gate run on its open `Flaky: <node id>` issue (a comment, or a new issue), and in the
# job summary. A flake whose issue call fails is recorded as failed in this run's record, so the next run
# reruns it and files it again; this run then exits 1.
# Usage: record-flakes.sh <this run's record> <the earlier record>
# Env: GH_REPO, EARLIER_RUN, EARLIER_SHA, PR, HEAD_SHA, THIS_RUN; GITHUB_STEP_SUMMARY when set.
set -uo pipefail
record=$1 earlier=$2
[ -s "$record/flakes" ] || exit 0
summary=${GITHUB_STEP_SUMMARY:-/dev/null}
here=$(cd "$(dirname "$0")" && pwd)
work=$(mktemp -d); trap 'rm -rf "$work"' EXIT
# The files that differ between the two trees, so a reader can tell a fix elsewhere from a real flake.
differ=$(python3 - "$here" "$earlier/tree" "$record/tree" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from affected_tests import listing
then, now = (listing(Path(p).read_text()) for p in sys.argv[2:])
paths = sorted(p for p in then.keys() | now.keys() if then.get(p) != now.get(p))
print(", ".join(paths[:20]) + (f" and {len(paths) - 20} more" if len(paths) > 20 else "") or "none")
PY
) || differ="unknown (a tree listing can't be read)"
# Without the open issues a new one could duplicate one, so every flake waits for the next run instead.
listed=yes
gh api --paginate "repos/$GH_REPO/issues?state=open&per_page=100" \
  --jq '.[] | select(.pull_request | not) | [.number, .title] | @tsv' > "$work/open-issues" < /dev/null || listed=
echo "### Flaky: failed in $EARLIER_RUN, passed on this rerun" >> "$summary"
: > "$work/unfiled"
while IFS= read -r test; do
  title="Flaky: $test"
  body="Failed in $EARLIER_RUN (head $EARLIER_SHA) and passed on the rerun in $THIS_RUN (head $HEAD_SHA), for #$PR. Files that differ between the two runs' trees: $differ."
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
done < "$record/flakes"
[ -s "$work/unfiled" ] || exit 0
python3 - "$record/lastfailed.json" "$work/unfiled" <<'PY'
import json, sys
path, unfiled = sys.argv[1], [t for t in open(sys.argv[2]).read().split("\n") if t]
try:
    failed = json.load(open(path))
except (OSError, ValueError):
    failed = {}
failed.update({test: True for test in unfiled})
json.dump(failed, open(path, "w"))
PY
echo 1 > "$record/exit"
exit 1
