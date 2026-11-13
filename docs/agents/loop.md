# Loop facts

Fork-only: upstream has no `docs/agents/loop.md`, so an upstream sync never conflicts with it. The `engineering-loop` skill reads it.

Worker model: mid for size/S or smaller

A tier, not a model id: Sonnet for Claude, resolved at launch to the newest generation. A small worker doesn't need the top tier, and the mid tier costs about half per item (agent-skills' trial). Leads and verifiers stay on the default. Undo: delete the line.

Cloud worker: claude-cloud, gate check:all

Workers code and test in Claude cloud sessions in the `omnigent-dev` environment, so the build and test load leaves the owner's machines; the lead and the Codex verifier stay on the host (the engineering loop's Cloud worker section). The light gate is the same `mise run check:all` that `local-ci` runs on the host. The worker also runs the pytest or vitest directories its change touches, as `.claude/skills/cloud-dev/SKILL.md` (In the VM) says, along with the VM's setup log, its `uv sync` line and the tests known to fail only in the VM. A cloud session clones the GitHub branch of the workspace it starts from, in the environment that workspace's `.claude/settings.local.json` names (`{"remote": {"defaultEnvironmentId": "<id>"}}`, the ID from `fork-env.sh OMNIGENT_DEV_ENVIRONMENT_ID`). The file is untracked, so the lead copies it into each new worktree and pushes the branch before it starts a worker. Undo: delete the line.

The loop's policy (`approvals.py`'s settings block) is in `docs/agents/gatekeeper.md` under `## Policy`.
