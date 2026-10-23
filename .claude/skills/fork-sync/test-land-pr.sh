#!/usr/bin/env bash
# land-pr.sh against a local bare "origin", a throwaway signing key and a stub gh.
set -euo pipefail
script="$(cd "$(dirname "$0")" && pwd)/land-pr.sh"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

ssh-keygen -q -t ed25519 -N '' -f "$tmp/key"
echo "t@t $(cut -d' ' -f1,2 "$tmp/key.pub")" > "$tmp/signers"
mkdir "$tmp/bin"
cat > "$tmp/bin/gh" <<'EOF'
#!/bin/sh
case "$1 $2 $4 $5" in
  "pr view -R $GH_REPO") printf 'OPEN\tmine\tfeat\t%s\tAdd thing\n' "${CROSS:-false}" ;;
  *) echo "unexpected gh $*" >&2; exit 1 ;;
esac
EOF
chmod +x "$tmp/bin/gh"
export PATH="$tmp/bin:$PATH"

git init -q --bare "$tmp/origin.git"
export GH_REPO="$(basename "$tmp")/origin"  # the owner/name land-pr.sh must read from origin's URL
git init -q -b mine "$tmp/fork"
cd "$tmp/fork"
git config user.name t; git config user.email t@t
git config gpg.format ssh; git config user.signingkey "$tmp/key"
git config commit.gpgsign true
export OMNIGENT_ALLOWED_SIGNERS="$tmp/signers"  # land-pr.sh verifies against this, not the checkout's file
git remote add origin "$tmp/origin.git"
git commit -q --allow-empty -m base
git push -q origin mine
git checkout -q -b feat
echo a > a; git add a; git commit -q -m one
echo b > b; git add b; git commit -q -m two
git push -q origin feat
git --git-dir="$tmp/origin.git" update-ref refs/pull/7/head feat
base=$(git rev-parse mine) feat=$(git rev-parse feat)

bash "$script" 7
landed=$(git --git-dir="$tmp/origin.git" rev-parse mine)
[ "$(git rev-parse "$landed^")" = "$base" ] || { echo "squash is not on top of mine" >&2; exit 1; }
[ "$(git rev-parse "$landed^{tree}")" = "$(git rev-parse "$feat^{tree}")" ] || { echo "squash tree differs from the PR" >&2; exit 1; }
git -c gpg.ssh.allowedSignersFile="$tmp/signers" verify-commit "$landed" 2>/dev/null || { echo "squash is not signed" >&2; exit 1; }
[ "$(git log -1 --format=%s "$landed")" = "Add thing (#7)" ] || { echo "wrong subject" >&2; exit 1; }
[ "$(git --git-dir="$tmp/origin.git" rev-parse feat)" = "$landed" ] || { echo "PR branch not at the squash" >&2; exit 1; }
echo "land-pr.sh lands a PR as one signed commit on mine"

# A PR that doesn't contain mine would undo what landed since: refused, mine unchanged.
git --git-dir="$tmp/origin.git" update-ref refs/pull/7/head "$feat"
code=0; bash "$script" 7 2>/dev/null || code=$?
[ "$code" = 1 ] && [ "$(git --git-dir="$tmp/origin.git" rev-parse mine)" = "$landed" ] \
  || { echo "a PR behind mine was not refused (exit $code)" >&2; exit 1; }
echo "land-pr.sh refuses a PR that doesn't contain mine"

# A PR from another repository could only end closed, not merged: refused before any push.
git --git-dir="$tmp/origin.git" update-ref refs/pull/7/head "$landed"
code=0; CROSS=true bash "$script" 7 2>/dev/null || code=$?
[ "$code" = 1 ] && [ "$(git --git-dir="$tmp/origin.git" rev-parse mine)" = "$landed" ] \
  || { echo "a cross-repository PR was not refused (exit $code)" >&2; exit 1; }
echo "land-pr.sh refuses a PR from another repository"

# A checkout that doesn't sign with SSH, or has no signing key, is refused before anything
# is fetched or pushed. The host's own git config is shut out so it can't supply either.
refs() { git --git-dir="$tmp/origin.git" for-each-ref; git for-each-ref; }
before=$(refs)
for unset in gpg.format user.signingkey; do
  git config --unset "$unset"
  code=0; err=$(GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 bash "$script" 7 2>&1) || code=$?
  git config gpg.format ssh; git config user.signingkey "$tmp/key"
  [ "$code" = 2 ] && [ "$(refs)" = "$before" ] \
    || { echo "a checkout without $unset was not refused before fetching (exit $code)" >&2; exit 1; }
  case "$err" in *"Landing PRs into mine"*) ;; *) echo "refusal without $unset garbled its guidance: $err" >&2; exit 1 ;; esac
done
echo "land-pr.sh refuses a checkout that doesn't sign with an SSH key"

# A squash signed by a key outside the trust list is refused after signing and before any push.
ssh-keygen -q -t ed25519 -N '' -f "$tmp/other"
echo "t@t $(cut -d' ' -f1,2 "$tmp/other.pub")" > "$tmp/other-signers"
code=0; OMNIGENT_ALLOWED_SIGNERS="$tmp/other-signers" bash "$script" 7 2>/dev/null || code=$?
[ "$code" = 1 ] && [ "$(refs)" = "$before" ] \
  || { echo "a squash outside the trust list was not refused before pushing (exit $code)" >&2; exit 1; }
echo "land-pr.sh refuses a squash signed outside the trust list"
