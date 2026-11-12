# Loop facts

Fork-only: upstream has no `docs/agents/loop.md`, so an upstream sync never conflicts with it. The `engineering-loop` skill reads it.

Worker model: mid for size/S or smaller

A tier, not a model id: Sonnet for Claude, resolved at launch to the newest generation. A small worker doesn't need the top tier, and the mid tier costs about half per item (agent-skills' trial). Leads and verifiers stay on the default. Undo: delete the line.

Cloud worker: claude-cloud, gate check:all

Workers code and test in Claude cloud sessions in the `omnigent-dev` environment, so the build and test load leaves the owner's machines; the lead and the Codex verifier stay on the host (the engineering loop's Cloud worker section). The light gate is the same `mise run check:all` that `local-ci` runs on the host. The worker also runs the pytest or vitest directories its change touches, as `.claude/skills/cloud-dev/SKILL.md` (In the VM) says, along with the VM's setup log, its `uv sync` line and the tests known to fail only in the VM. A cloud session clones the GitHub branch of the workspace it starts from, in the environment that workspace's `.claude/settings.local.json` names (`{"remote": {"defaultEnvironmentId": "<id>"}}`, the ID from `fork-env.sh OMNIGENT_DEV_ENVIRONMENT_ID`). The file is untracked, so the lead copies it into each new worktree and pushes the branch before it starts a worker. Undo: delete the line.

## Settings

```yaml
loop:
  ci: false
  delegate: all

land:
  signed_squash_to: mine
  allowed_signers: ~/.config/omnigent/allowed_signers
```

`loop.delegate: all`: the delegated approver's go counts for a PR into `mine` too, so a low or medium risk PR lands after the gate and the verifier without waiting for the owner. A `risk:high` PR still waits for the owner's own go on its head: the tool would accept the delegate's go there, and the delegated approver doesn't give one. Undo: delete the line, which also withdraws the delegate's goes on open PRs.

`land`: every machine's `omnigent-update` and the nightly sync refuse a `mine` head that a key outside the allowed signers signed, and GitHub signs its own merges with GitHub's key. So `approvals.py land` squashes and signs the PR in the landing host's checkout (AGENTS.md, This fork: landing PRs into `mine`).

`loop.ci: false`: the fork runs no GitHub Actions, so `approvals.py local-ci <pr>` is the merge proof: it runs `mise run check:all` from the root `mise.toml`, which is fork-only and holds tasks only (no tools or env). Upstream has no `mise.toml`; a sync conflict there keeps ours. The gate first runs `check:setup`, which installs what the hooks need (the Python environment and the web packages, which custom-lint's TypeScript check needs for any change), so it also passes in the fresh worktree `land` gates in; it is quick when they are installed.
