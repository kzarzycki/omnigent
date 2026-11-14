# Gatekeeper policy

Fork-only: upstream has no `docs/agents/gatekeeper.md`, so an upstream sync never conflicts with it. The `engineering-loop` skill's `approvals.py` reads the block below.

## Policy

```yaml
spec:
  auto:
    - paths:   # fork tooling only: the gate, loop settings, agent docs, the fork's own skills
        - mise.toml
        - docs/agents/loop.md
        - AGENTS.md
        - CLAUDE.md
        - .claude/skills/cloud-dev/**
        - .claude/skills/fork-sync/**

loop:
  ci: false
  delegate: all
```

`spec.auto` `paths`: the owner's standing decision of 2026-11-12 approves, without asking, specs for fork tooling whose risk is not high; product behaviour (chat, server, runner, bridge, web) and anything `risk:high` still go to the owner. The lead labels such an issue `spec:paths`, and `check build` and `check merge` approve it only while every path the PR touches, a moved file's old name included, matches a glob here. The globs name the gate (`mise.toml`), the loop facts and agent docs (`docs/agents/loop.md`, `AGENTS.md` and its `CLAUDE.md` link) and the fork-only skills; the skills upstream ships under `.claude/skills/` and the claude-cloud bridge in `integrations/` stay outside. `docs/agents/gatekeeper.md` stays outside too, so a change to this policy always goes to the owner: a PR the globs approve could otherwise widen them. Globs are fnmatch, where `*` also matches `/`. Undo: delete the `spec` section.

`loop.delegate: all`: the delegated approver's go counts for a PR into `mine` too, so a low or medium risk PR lands after the gate and the verifier without waiting for the owner. A `risk:high` PR still waits for the owner's own go on its head: the tool would accept the delegate's go there, and the delegated approver doesn't give one. Undo: delete the line, which also withdraws the delegate's goes on open PRs.

`approvals.py land`: there is no `land` section, so a PR lands as a GitHub merge, pinned to the head the gates checked. GitHub is the trust anchor: no signature is checked on `mine`, by `approvals.py`, `omnigent-update` or the sync (AGENTS.md, This fork: landing PRs into `mine`).

`loop.ci: false`: the fork runs no GitHub Actions, so `approvals.py local-ci <pr>` is the merge proof: it runs `mise run check:all` from the root `mise.toml`, which is fork-only and holds tasks only (no tools or env). Upstream has no `mise.toml`; a sync conflict there keeps ours. The gate first runs `check:setup`, which installs what the hooks need (the Python environment and the web packages, which custom-lint's TypeScript check needs for any change), so it also passes in the fresh worktree `land` gates in; it is quick when they are installed.
