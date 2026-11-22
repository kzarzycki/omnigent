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
  delegate: all

land:
  queue: mergify
  merge_commit_heads:
    - "sync/*"
```

`spec.auto` `paths`: the owner's standing decision of 2026-11-12 approves, without asking, specs for fork tooling whose risk is not high; product behaviour (chat, server, runner, bridge, web) and anything `risk:high` still go to the owner. The lead labels such an issue `spec:paths`, and `check build` and `check merge` approve it only while every path the PR touches, a moved file's old name included, matches a glob here. The globs name the gate (`mise.toml`), the loop facts and agent docs (`docs/agents/loop.md`, `AGENTS.md` and its `CLAUDE.md` link) and the fork-only skills; the skills upstream ships under `.claude/skills/` and the claude-cloud bridge in `integrations/` stay outside. `docs/agents/gatekeeper.md` stays outside too, so a change to this policy always goes to the owner: a PR the globs approve could otherwise widen them. Globs are fnmatch, where `*` also matches `/`. Undo: delete the `spec` section.

`loop.delegate: all`: the delegated approver's go counts for a PR into `mine` too, so a low or medium risk PR lands after the gate and the verifier without waiting for the owner. A `risk:high` PR still waits for the owner's own go on its head: the tool would accept the delegate's go there, and the delegated approver doesn't give one. Undo: delete the line, which also withdraws the delegate's goes on open PRs.

`land.merge_commit_heads`: an upstream-sync PR (head `sync/<upstream sha>`, a two-parent merge of upstream's `main` into `mine`) lands as a merge commit, so `mine` keeps upstream's parent and the next sync's merge base moves forward; a squash would drop that parent and make every later sync replay the same upstream commits. Every other PR lands as a squash, which the `default` and `sync` queue rules in `.mergify.yml` carry out. GitHub is the trust anchor: no signature is checked on `mine`, by `approvals.py`, `omnigent-update` or the sync (AGENTS.md, This fork: landing PRs into `mine`). Undo: delete the `land` section; a sync PR then can't land through `land` without losing its upstream parent.

`land.queue: mergify`: `approvals.py land` takes no lock, runs no host gate and merges nothing: it posts the `land` status on the PR's head and adds `loop:queued`, and Mergify merges (below). A cloud session lands this way too. Undo: delete the line; `land` then merges through GitHub itself, pinned to the head the gates checked, and runs the gate on the host.

No `loop.ci` line (its default, `true`): the required check is the proof. The `mine` ruleset requires the `gate` check (`.github/workflows/gate.yml`: `mise run check-fast`, the pre-commit hooks, the skills' tests and the tests the change touches), which runs on each PR head (the hooks, the skills' tests and only the test files the PR changes: `AFFECTED_TESTS_NARROW`) and, with the full affected-tests set, once per Mergify batch (up to 5 PRs, 5 minutes' wait, a red batch split to find the bad PR) on the draft PR that tests the merge. `check merge` and `land` wait on that check and run no `local-ci` of their own, so a tree is proven once, in GitHub Actions. The full suite runs nightly (`fork-nightly.yml`). Undo: set `loop.ci: false`, which makes `approvals.py local-ci <pr>` the merge proof again.

## Queueing a PR (Mergify)

Mergify executes the merge; our tooling decides. After `approvals.py check merge <pr>` passes (the proofs, and the `gate` check green on the head), `land` posts the `land` commit status on the PR's head and adds `loop:queued`. `.mergify.yml` takes a PR in only with both: it tests it on a draft PR on top of `mine`, where `gate` runs again on the merged tree, then merges it: a squash, or a merge commit for a `sync/*` head. A push drops `land`, so the PR leaves the queue until `land` posts it again. A PR Mergify drops gets `loop:dequeued`, and the next `land` sends it back to draft. `@mergifyio queue` stays restricted to write access.
