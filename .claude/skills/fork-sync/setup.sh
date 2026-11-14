#!/usr/bin/env bash
# Prepare this checkout for a fork sync: the `upstream` remote, an identity, and unsigned
# commits. GitHub is the trust anchor, so nothing here needs a signer list or key.
#
# exit 0 ready, 2 not the fork checkout.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

git remote get-url origin >/dev/null 2>&1 || { echo "!! no origin remote" >&2; exit 2; }
git remote get-url upstream >/dev/null 2>&1 \
  || git remote add upstream https://github.com/omnigent-ai/omnigent.git

# Cloud sessions can come with a platform signer configured; the sync commits unsigned.
git config commit.gpgsign false
git config user.name "omnigent fork sync"
git config user.email "k.zarzycki@gmail.com"
# Cloud sessions start from a shallow clone; the delta needs the merge base.
[ "$(git rev-parse --is-shallow-repository)" = false ] || git fetch -q --unshallow origin
git fetch -q origin mine
git rev-parse -q --verify refs/heads/mine >/dev/null || git branch -q mine origin/mine
echo "==> ready"
