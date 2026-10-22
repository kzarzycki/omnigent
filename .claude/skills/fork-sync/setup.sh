#!/usr/bin/env bash
# Prepare this checkout for a fork sync: the `upstream` remote, the signer list,
# and signing with the dedicated sync key.
#
# The signer list and key come from the environment, never from the repo: a
# commit pushed to `mine` by someone else could change files here, but not them.
#
#   OMNIGENT_SYNC_ALLOWED_SIGNERS  base64 of an allowed_signers file
#   OMNIGENT_SYNC_SIGNING_KEY      base64 of the private key listed in it
#
# exit 0 ready, 2 not the fork checkout, 3 missing or untrusted key, 4 the
# checked-out commit (and so this skill) is not signed by an allowed key.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

git remote get-url origin >/dev/null 2>&1 || { echo "!! no origin remote" >&2; exit 2; }
git remote get-url upstream >/dev/null 2>&1 \
  || git remote add upstream https://github.com/omnigent-ai/omnigent.git

[ -n "${OMNIGENT_SYNC_ALLOWED_SIGNERS:-}" ] && [ -n "${OMNIGENT_SYNC_SIGNING_KEY:-}" ] \
  || { echo "!! OMNIGENT_SYNC_ALLOWED_SIGNERS / OMNIGENT_SYNC_SIGNING_KEY not set" >&2; exit 3; }
umask 077
dir="$HOME/.config/omnigent-sync"
mkdir -p "$dir"
printf %s "$OMNIGENT_SYNC_ALLOWED_SIGNERS" | base64 -d > "$dir/allowed_signers"
printf %s "$OMNIGENT_SYNC_SIGNING_KEY" | base64 -d > "$dir/key"
pub=$(ssh-keygen -y -f "$dir/key" | awk '{print $2}')
grep -q " $pub" "$dir/allowed_signers" \
  || { echo "!! the signing key is not in the allowed signers" >&2; exit 3; }

git config gpg.ssh.allowedSignersFile "$dir/allowed_signers"
git verify-commit HEAD 2>/dev/null \
  || { echo "!! HEAD $(git rev-parse --short HEAD) is not signed by an allowed key; not running" >&2; exit 4; }

# Cloud sessions sign through the platform's signer; this job signs as itself.
git config gpg.format ssh
git config gpg.ssh.program ssh-keygen
git config user.signingkey "$dir/key"
git config commit.gpgsign true
git config user.name "omnigent fork sync"
git config user.email "k.zarzycki@gmail.com"
git fetch -q origin mine
git rev-parse -q --verify refs/heads/mine >/dev/null || git branch -q mine origin/mine
echo "==> ready: signing as $(ssh-keygen -lf "$dir/key" | awk '{print $2}')"
