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

timeouts:
  run: 4h

select:
  build_inputs:
    - pyproject.toml
    - uv.lock
    - mise.toml
    - tests/conftest.py
    - tests/_helpers/**
    - tests/helpers/**
    - tests/_fixtures/**
    - tests/fixtures/**
    - tests/data/**
    - .github/**
  build_inputs_in_queue: floor
```

`spec.auto` `paths`: the owner's standing decision of 2026-11-12 approves, without asking, specs for fork tooling whose risk is not high; product behaviour (chat, server, runner, bridge, web) and anything `risk:high` still go to the owner. The lead labels such an issue `spec:paths`, and `check build` and `check merge` approve it only while every path the PR touches, a moved file's old name included, matches a glob here. The globs name the gate (`mise.toml`), the loop facts and agent docs (`docs/agents/loop.md`, `AGENTS.md` and its `CLAUDE.md` link) and the fork-only skills; the skills upstream ships under `.claude/skills/` and the claude-cloud bridge in `integrations/` stay outside. `docs/agents/gatekeeper.md` stays outside too, so a change to this policy always goes to the owner: a PR the globs approve could otherwise widen them. Globs are fnmatch, where `*` also matches `/`. Undo: delete the `spec` section.

`loop.delegate: all`: the delegated approver's go counts for a PR into `mine` too, so a low or medium risk PR lands after the gate and the verifier without waiting for the owner. A `risk:high` PR still waits for the owner's own go on its head: the tool would accept the delegate's go there, and the delegated approver doesn't give one. Undo: delete the line, which also withdraws the delegate's goes on open PRs.

`land.merge_commit_heads`: an upstream-sync PR (head `sync/<upstream sha>`, a two-parent merge of upstream's `main` into `mine`) lands as a merge commit, so `mine` keeps upstream's parent and the next sync's merge base moves forward; a squash would drop that parent and make every later sync replay the same upstream commits. Every other PR lands as a squash, which the `default` and `sync` queue rules in `.mergify.yml` carry out. GitHub is the trust anchor: no signature is checked on `mine`, by `approvals.py`, `omnigent-update` or the sync (AGENTS.md, This fork: landing PRs into `mine`). Undo: delete the `land` section; a sync PR then can't land through `land` without losing its upstream parent.

`land.queue: mergify`: `approvals.py land` takes no lock, runs no host gate and merges nothing: it posts the `land` status on the PR's head and adds `loop:queued`, and Mergify merges (below). A cloud session lands this way too. Undo: delete the line; `land` then merges through GitHub itself, pinned to the head the gates checked, and runs the gate on the host.

`timeouts.run: 4h`: one selection run, the queue's check included, gets 4 hours: a whole-suite run takes about 200 minutes plus setup. Every other limit follows it: Mergify's `checks_timeout` (`.mergify.yml`), the `gate` job's `timeout-minutes` (`gate.yml`, marked `# timeouts.run`) and `land --wait`, which waits the queue's timeout plus one more run (8 hours). `gatekeeper.py timeouts --check` names each one out of step. Undo: delete the `timeouts` section; the limits fall back to 1 hour, which the full suite exceeds.

`select.build_inputs` with `build_inputs_in_queue: floor`: in the merge queue a build input (the dependency files, the toolchain pin, the shared test setup, `.github/**`) selects the other changed files' tests plus, for `uv.lock`, the tests importing a package whose version moved, instead of the whole ~200 minute suite (`gate.yml` sets `AFFECTED_TESTS_BUILD_INPUTS=floor`, which `.claude/skills/cloud-dev/affected_tests.py` reads). The full suite then runs after the merge: `fork-nightly.yml` (marked `# timeouts: nightly`) also runs on a push to `mine` that touches a build input. Undo: delete the `select` section and `AFFECTED_TESTS_BUILD_INPUTS` in `gate.yml`; a build input then runs the whole suite in the queue again.

No `loop.ci` line (its default, `true`): CI is the proof, and it runs once per change, in the merge queue. `gate` (`.github/workflows/gate.yml`: `mise run check-fast`, the pre-commit hooks, the skills' tests and the tests the change touches) runs only on the draft PR Mergify opens to test the merge, and `.mergify.yml` merges only once it is green there (`merge_conditions`). The `mine` ruleset requires no status check, so `check merge` and `land` wait on no CI run and run no `local-ci` of their own: a PR's own head is never tested, since the queue tests the tree that actually merges. The full suite runs nightly (`fork-nightly.yml`). Undo, to test each PR head as well: add the `gate` required check back to the `mine` ruleset (`landing (gate via PR)`), drop the job's `if:` in `gate.yml`, and drop `merge_conditions` from both queue rules. To prove merges on the host instead, set `loop.ci: false`, which makes `approvals.py local-ci <pr>` the merge proof again.

## Queueing a PR (Mergify)

Mergify executes the merge; our tooling decides. After `approvals.py check merge <pr>` passes (the proofs, the verifier and the go; no CI run), `land` posts the `land` commit status on the PR's head and adds `loop:queued`. `.mergify.yml` takes a PR in only with both: it tests it on a draft PR on top of `mine`, where `gate` runs on the merged tree, and merges it only once `gate` is green there: a squash, or a merge commit for a `sync/*` head. A red `gate` drops the PR from the queue. A push drops `land`, so the PR leaves the queue until `land` posts it again. A PR Mergify drops gets `loop:dequeued`, and the next `land` sends it back to draft. `loop:dequeued` is no hold, since Mergify removes it about a minute later; a hold is the `loop:hold` label plus a PR comment saying who holds it and why. Mergify won't queue a held PR and `land` refuses it; the holder removes the label before landing. A coordinator claiming a PR from other leads uses the same hold. `@mergifyio queue` stays restricted to write access.
