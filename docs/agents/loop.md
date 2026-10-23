# Loop facts

Fork-only: upstream has no `docs/agents/loop.md`, so an upstream sync never conflicts with it. The `engineering-loop` skill reads it.

Worker model: mid for size/S or smaller

A tier, not a model id: Sonnet for Claude, resolved at launch to the newest generation. A small worker doesn't need the top tier, and the mid tier costs about half per item (agent-skills' trial). Leads and verifiers stay on the default. Undo: delete the line.

Land: local signed squash to mine, allowed signers ~/.config/omnigent/allowed_signers

Every machine's `omnigent-update` and the nightly sync refuse a `mine` head that a key outside the allowed signers signed, and GitHub signs its own merges with GitHub's key. So `approvals.py land` squashes and signs the PR in the landing host's checkout (AGENTS.md, This fork: landing PRs into `mine`).

CI: none

The fork runs no GitHub Actions, so `approvals.py local-ci <pr>` is the merge proof: it runs `mise run check:all` from the root `mise.toml`, which is fork-only and holds tasks only (no tools or env). Upstream has no `mise.toml`; a sync conflict there keeps ours.
