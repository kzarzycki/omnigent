# Policy

gatekeeper.md holds the gatekeeper's policy, a fenced `yaml` block under `## Policy`, which
`approvals.py` reads, so a team that takes only the gatekeeper writes no loop file; loop.md
keeps the prose for people and agents: why a path is critical, how to judge a finding, the
risk criteria in words. A project without gatekeeper.md may keep the block under loop.md's
`## Settings` for now: `approvals.py` reads it there with a deprecation line, and refuses a
project with both, since two sources of the bar would drift. `uv run --python '>=3.11' approvals.py check setup` is a consumer's gate on a pack upgrade: it reads the policy this way and
scans the repo's mise config and workflows for a pack script run without `uv run` (README § Upgrading the pack). `approvals.py` reads these files,
and issue-tracker.md, from the base's copy on `origin/<base>`, never the checkout's: the base the
PR a command acts on merges onto, the ref `land` builds on (`check`, `land`, `local-ci <pr>`,
`approve merge`, `delegate`, `brief`), else the default branch. `land` fetches the base before
it reads the policy (where it may run, its key, which drafts to ready) and reads it again after
its fetch under the landing lock, so a policy another land changed meanwhile decides the batch:
it places the land again (refused, the lock released, where the base now excludes the place it
runs; signed with the key it now names), and a draft it readied that the new policy would not
ready goes back to draft. A failed fetch before the lock readies no draft. So a PR can't
loosen its own gate and a checkout behind its
base can't hold an old one: a working-tree copy that differs gets one line naming the file and
both commits, and only a checkout without that ref (no `origin`) reads its own, with a line
saying so. A PR that changes the gate is therefore gated, recorded and landed under the base's
gate, and its own applies from the next PR on. The block is YAML because a person
reads and edits it by hand as `key: value` under section names, a list one item per line,
and it is typed (a boolean, a number and a list are each their own kind, so none is a
string the reader guesses at). A string needs no quotes unless YAML would read it otherwise:
one that starts with `*`, `&`, `!` or `[`, holds `: ` or ` #`, or reads as another type.
YAML 1.1, which PyYAML follows, reads `yes`, `no`, `on` and `off` as booleans and `1.10` as
the number 1.1, so `approvals.py` matches each value's type exactly and refuses, naming the
key, a value or name YAML read as something else: quote it. A value names a thing (a label,
a glob, a skill, a branch, a kind of spec), never a rule in words: which specs the policy
approves is a list of kinds, not a phrase such as `auto unless risk` whose meaning lives in
the reader, so a person reads the policy off the block and may name any part of it.
`approvals.py` refuses, naming the key, a section or key it doesn't know and a value of the
wrong type, and refuses a policy file without the block, so a mistyped setting is never read as
a rule nobody wrote.

The scripts read the block with PyYAML, the one package they need beyond the standard
library. Each declares it inline (PEP 723), so `uv run <this skill's folder>/scripts/approvals.py` installs it
into uv's cache on first use and nothing is installed by hand; uv comes with the project's
mise. Run with a bare `python3` that lacks PyYAML, a script exits with one line saying to
run it with `uv run`. They need Python 3.9 or later, except `check setup`, which needs 3.11 (it reads mise's TOML). Every key is optional:

```yaml
spec:
  # specs approved by policy, with no owner record; any other waits for the owner, but a migration
  # row its approved parent story covers, whatever this says (SKILL.md, Specs).
  # `auto: true` approves every spec, and by default the risk rule then holds the spec, not the merge
  auto:
    - bug
    - follow-up
    - sub-issue
    - paths:   # a spec labelled spec:paths whose PR stays inside these globs
        - mise.toml
        - docs/agents/**

loop:
  cap: 3
  ci: false
  gate: GATE_TIER=full mise run check:all   # the merge check, and the variables it sets
  local_ci_env:
    - GATE_TIER
  opt_out: true
  owner: octocat   # whose own approved:spec label, added after the spec's last edit, is their approval
  machine_exit: 78   # the gate's exit for "this machine isn't ready": local-ci refuses it as a machine fault
  short_seconds: 120   # a job expected to finish within this takes the host's short lane; 0 turns the lane off
  pre_land: python3 scripts/check_fork.py   # land's check of each PR against the base, before it batches them
  delegate: all   # the delegated approver's go on a head covers a PR into the default branch too, and their spec or plan approval counts, but on risk.critical_paths; default feature
  delegate_spec: true   # false keeps spec and plan approvals the owner's under delegate: all, whose merge go still counts; default true
  pack_staleness: refuse   # approvals.py exits 1 at start when its checkout is behind its upstream and the script changed there (default warn; omnigent_agent.py only warns)
  stalled_after_hours: 12   # check stalled lists a verified PR still missing its go or record this long after its head's push (default 4)
  head_proof:      # the status land requires on each head, and the names its recording takes from the caller
    status: gate-ui
    env:
      - GATE_UI_TESTS

practice:
  plan: writing-plans

risk:
  verifier: true
  labels:
    - size:L
    - size:XL
    - area:billing
  paths:
    - .github/**
    - CODEOWNERS
  critical_paths:   # the `critical` level, whatever the labels and the verifier say; was merge_paths
    - src/orders/**

levels:   # what each level asks; the values shown are the defaults
  low:
    spec: policy
    merge: gates
    rerun: false   # the worker's record is the proof
  high:   # under `spec.auto: true` {spec: owner, merge: gates}
    spec: policy
    merge: go
    rerun: true    # the merge waits for an independent rerun of the head's selection
  critical:
    spec: policy
    merge: owner
    rerun: true

plan:
  labels:
    - size:XL

signoff:
  labels:
    - area:ios

bot:
  dependabot[bot]:
    paths:
      - uv.lock
      - "**/package.json"   # quoted: a leading * is a YAML alias
  renovate:                 # an updater under a person's token: named by its branch
    branch: renovate/
    paths:
      - uv.lock
  pack-bump:                # the engineering pack's bump, which a cloud worker can push
    branch: claude/ready-pack-bump-
    paths:
      - apm.yml
      - apm.lock.yaml
      - ".agents/**"
      - ".claude/**"        # every folder the pack sync writes its copies to

land:
  signed_squash_to: main
  allowed_signers: ~/.config/git/allowed_signers
  batch_wait_seconds: 600   # land waits up to this long before it merges, while another PR waits only on its required checks; default 0
  # wait: 4h30m             # deprecated: `land --wait`'s ceiling is derived from `timeouts.run` below; a set value still wins
  # queue: mergify   # land hands each landable PR to a Mergify merge queue (`.mergify.yml`, `approvals.py queue-config`'s render of this skill's mergify.yml) instead; refused with signed_squash_to
  # batch_size: 3    # with `queue: mergify`, the PRs per queue batch (1: one PR per entry); queue-config renders it and batch_wait_seconds (as the batch's wait) into `.mergify.yml`
  # queue: github    # the base's ruleset requires GitHub's merge queue: land hands PRs to it as it would anyway, and refuses the setting where the ruleset has no merge_queue rule

timeouts:   # every time limit of a run; the rest are derived from these (`gatekeeper.py timeouts` prints them)
  test: 60s   # one test: the project maps it onto its runner's per-test option in loop.md's `Affected tests:` command
  run: 60m    # one selection run, the queue's check included

carry_over_main:
  # a clean merge of main keeps the head's verdict unless either side changed one of these
  except:
    - mise.toml
    - uv.lock
    - '**/uv.lock'
    - pyproject.toml
    - .github/**
```

| Section | Key | Value | Without it |
|---|---|---|---|
| `spec` | `auto` | a list of `bug`, `follow-up`, `sub-issue` and `paths` terms, or `true` for every spec (Approvals and proof) | every spec waits for the owner |
| `loop` | `cap` | a whole number (The cap) | the default |
| | `ci` | deprecated, goes in the next release (Approvals and proof); `false` for a project without CI, whose `local-ci --record` then runs the narrowed check against the merge base in narrowed mode; `check merge` and `land` ask no `local-ci` pass under it (#1872) | `true` |
| | `gate` | the merge check `local-ci` runs: a command, with any variables it sets in front (Adopting the loop) | `mise run check:all` |
| | `gate_steps` | the merge check as steps, in place of `gate`: a list of `name`, `command` (parsed as `gate` is), `where` (`any`; `host`: never run in a cloud session; or `linux`: only where `sys.platform` is Linux, a Linux host or a cloud session, never macOS) and optionally `paths`, globs of the files the step reads, matched on the full path as `paths` are, so its pass is reused on any tree whose files under them are the same, and optionally `on_demand: true`, a step `local-ci` and `local-ci --record` skip, saying so, and `land` never needs, which runs only when named (`local-ci --record --step <name>`) and is keyed, recorded and reused as any step: a check too costly for every PR, refused on every step, since the gate would then have no step (#2295); each step's pass recorded and reused by itself (Adopting the loop) | the one `gate` command; a step without `paths` reads the whole tree |
| | `local_ci_env` | the environment variables the check reads, cleared for its run unless `gate` sets them (Adopting the loop) | none |
| | `machine_exit` | the gate's exit that means the machine isn't ready (a locked screen, low disk), such as 78: `local-ci` refuses it as a machine fault (`approvals.py`'s docstring); `land` runs no gate, so it reads none | every non-zero exit is red |
| | `short_seconds` | a whole number of seconds: a job expected to finish within it takes the host's short lane instead of the heavy slot, and `0` turns the lane off (Rules, One heavy gate per host slot) | `120` |
| | `opt_out` | `true` where a person may work outside the loop (Approvals and proof) | `false` |
| | `pre_land` | a check `land` runs for each PR before batching it, parsed as `gate` is, from the base's tree with `LAND_HEAD` and `LAND_BASE` set, such as one refusing a branch forked before a restructuring; non-zero leaves that PR out of the batch with one comment per head holding its output, and it stays ready; `check merge` runs it too, as a wait, so it never calls clear a PR `land` would leave out (`approvals.py`'s docstring says how) | no check |
| | `head_proof` | `status`, a commit status `land` requires on each head, and `env`, the variables `local-ci --record --head-proof` takes from the caller for that run, both required (Approvals and proof) | no head proof is asked |
| | `rerun_pusher` | the account whose push of a rerun record counts as the CI's rerun (Independent rerun, below), such as `my-ci-app[bot]` | `github-actions[bot]` |
| | `stalled_after_hours` | a whole number of hours: `check stalled` lists an open PR whose newest verifier pass on its head is satisfied (or at the cap) but that still lacks the go its rating asks or the `head_proof` record, once both its head's push and its newest verdict on that head are more than this long ago, with neither coming in, naming what it misses (`missing: go`, `missing: record`) | `4` |
| `mutate` | `inconclusive_exit` | a whole number from 1 to 255: the exit a test command returns for "setup failed, no verdict", which `mutate.py` reports as an inconclusive patch (never killed) and exits 3 for; the project's test command exits with it when its device or build setup fails (verifier.md, Mutation-check the new tests) | `75` |
| `select` | `unkeyed`, `build_inputs` (globs of the project's build inputs: source dirs, build files, dependency locks, toolchain pins) and `checks`, a list of `paths` and `command`, each command in one row only (a policy naming one twice is refused); all optional, and `checks` does nothing without `build_inputs` | `unkeyed` is a list of checks, each a `gate_steps` step's `name` or `command`, or `gate`'s command: checks that read what no record key covers (a live service, a credential that rotates, the host itself), so the same tree can give another result. `local-ci`, `local-ci --record` and `land` run a listed check every time and never reuse its pass, and `queue-select` prints it for every queue batch; `local-ci <pr>` without a run names it as the step to run. `build_inputs` cuts `affected-tests` and the narrowed gate (`local-ci --record --base`, `local-ci --record` without `--base` and `local-ci <pr>` under `ci: false`) by build input before the `Affected tests:` command runs: a change with any build input runs that command; one whose every file is non-product (matching no `build_inputs` but the pack's set `docs/*`, `*.md`, `.agents/*`, `.claude/*`, `apm.yml`, `apm.lock.yaml`, or a check's `paths`) skips it and runs each check whose `paths` it touches, none passing at once, so a docs, skills or pack-bump change skips the build and the suites; such a narrowed record's command names the checks it ran (`select.checks: <command>...`, or `none`). The change is the head's commits since the base: `local-ci <pr>`, `land` and the merge queue read the PR's head from any checkout, and a checkout with uncommitted or untracked changes, or a diff git can't list, runs the `Affected tests:` command (`local-ci --record` then records nothing, as on any checkout with changes). A moved file counts under both its names. A file matching neither, `gatekeeper.md` and `loop.md` count as build inputs, since a path whose effect on the build is unclear tests; a project whose tests or build read a file under `docs/`, `.claude/` or `.agents/`, or any `.md`, lists it in `build_inputs` | every step's pass is reused on its key, and every change runs the `Affected tests:` command; `select` without `build_inputs` cuts nothing |
| `select` | `shards` | a whole number of at least 1: how many disjoint shards a whole-suite selection splits into, balanced by the durations the last full run recorded, each shard run as its own job by the project's CI and recorded as its own pass keyed by the tree and the shard's command; the run passes when every shard has a pass on the tree. Only a whole-suite selection splits (a build input changed, over half the suite, the nightly), or one whose estimate, the sum of its items' recorded seconds (an unrecorded item at the mean of the recorded ones), is over `timeouts.run`; a narrow selection under it never does, and with no recorded durations there is no estimate. `approvals.py shards --suite <file> [--selection <file>] [--whole] [--durations <json of seconds per item>]` prints the split (one `# shard i/n` block each; a count above the items is capped). A CI job for shard `i` runs `approvals.py shards ... --record <head> --shard <i> --command <cmd>`: it runs `<cmd>` with the shard's items as its last arguments, on the checkout at `<head>`, and on exit 0 pushes the shard's pass as R1's record branch (the same record `local-ci --record` pushes, keyed by the tree and the shard's command). The gate asks `approvals.py shards ... --passed <head> --command <cmd>`, which exits 0 only when every shard has that pass. The project maps a shard onto its runner here and in its CI | `1`: one selection, one runner, as before |
| `select` | `build_inputs_in_queue` | `floor` or `full`: what the merge queue's selection does with a build input (#2445). `floor` (default): the other changed files' tests, plus, for a `uv.lock` the project's selector can read, the tests that import a package whose version moved; other build inputs add nothing in the queue, and a nightly workflow that also triggers on a push to main touching a `build_inputs` path covers them after the merge (`timeouts --check` fails when that workflow, marked `# timeouts: nightly`, lists other push paths). `full`: a build input selects the whole suite, as before | `floor` |
| `practice` | `intent`, `spec`, `plan`, `build`, `review`, `land` | the skill that fills that stage (Practice) | the stage's default |
| `risk`, `plan`, `signoff` | `always` (`true` or `false`), `labels`, `paths`, and in `risk` `verifier` | a rule (Approvals and proof); `plan` has no `paths`, since a plan comes before the change | no rule |
| `risk` | `critical_paths` | globs matched on the PR's files, as `paths` are: a PR touching one rates `critical` (Approvals and proof); `merge_paths` is its deprecated name, read with a `deprecated:` line, and both set is refused | nothing rates `critical` |
| `incubating` | `append_paths` (globs of existing files a feature may only append lines to), `waive` (any of `evidence`, `proof_run`, `verifier`, `full_gate`, `coverage`) | what an eligible `incubating` PR stops owing (Approvals and proof, Incubating) | the mode is off; with the section and no `waive`, all five |
| `levels` | `low`, `high`, `critical`, each with `spec` (`policy` or `owner`), `merge` (`gates`, `go` or `owner`) and `rerun` (`true` or `false`) | what a change rated at that level needs (Approvals and proof); a key left out keeps its default. `rerun: true` makes `check merge` and `land` wait for an independent rerun of the head's selection (Independent rerun, below) | `low` {policy, gates, rerun false}; `high` {policy, go, rerun true}, or under `spec.auto: true` {owner, gates, rerun true}; `critical` {policy, owner, rerun true} |
| `bot.<login>` | `paths`, and `branch` (a head-branch prefix) to name an updater by its branch instead | a bot whose PR needs no verifier review (Approvals and proof); under `branch`, a commit author with no GitHub account (a cloud worker's) counts as the PR's author, and a PR whose `apm.yml` changes only the `ref:` line of its `kzarzycki/agent-skills/engineering` dependency, to a release (a version, a range of one or the `engineering-v` tag, never a branch or a commit), the rest of the diff matching `paths`, needs no spec approval either (a pack bump) | every PR needs one |
| `land` | `signed_squash_to` and `allowed_signers`, set together or not at all; `where` (`host`, `claude-cloud` or `both`; `cloud` still means `claude-cloud`), `signing_key_env`, `merge_queue`, `merge_commit_heads` (a list of fnmatch patterns over head branch names, such as `sync/*`), `batch_wait_seconds` (a whole number of seconds, 0 or more; under `queue: mergify` also the queue batch's wait), `batch_size` (a whole number, 1 or more: the Mergify queue's PRs per batch, 1 for one PR per entry), `wait` (deprecated: a duration such as `4h30m`, which still overrides the ceiling `timeouts` derives, with a `deprecated:` line) and `queue` (`mergify` or `github`) | for a repo that trusts only commits its maintainers sign, the branch and the allowed signers file: `land` then squashes and signs the PR in the checkout instead of merging it on GitHub; `where` is where `land` may run (default `both`: a cloud session lands too, with no `land` section on a base whose ruleset requires GitHub's merge queue), and in a cloud session a repo with `signed_squash_to` signs with the key in the variable `signing_key_env` names, which must be in the allowed signers, never the session's own, and lands a PR there only with `merge_queue: false`, since REST can't say whether the base requires a merge queue (`approvals.py`'s docstring says how); a base whose ruleset requires GitHub's merge queue needs no key there, since GitHub merges and signs; `batch_wait_seconds` makes `land`, holding the lock, wait up to that long before it merges the batch while another open PR into the same base is held by nothing but its required checks still running (the proofs and any owner go already hold, and a PR whose required check no workflow will run, a conflicting one or a readied draft whose workflow lacks `ready_for_review`, never counts), and each such PR whose checks turn green in that time joins the batch; its wait and who joined are printed and shown on the `Landed` comment's `Batch:` line, and `land` of named PRs only never waits; `land --wait` waits in all for the ceiling `timeouts` derives (below), or `wait` where a project still sets it, whatever the lock's own staleness (2 hours); a wait that reaches it leaves the event `waiting` for a PR a queue or its running checks still move, else `needs-owner`; no gate runs at `land`, on the batch or on the base's new tip: a PR merges on the verifier's record on its head (#1872), and the old keys `gate_on`, `cloud_gate_timeout_seconds` and `host_gate` are ignored, each with one `ignored:` line on stderr; it is optional, so it needs no signing keys; `queue: mergify` names a Mergify merge queue as the base's queue, on a base whose rules require no GitHub merge queue (they win): `land` posts the `land` status on each candidate's head and adds `loop:queued`, which the repo's `.mergify.yml` queue conditions read (they also exclude a PR carrying `loop:hold`, which `land` never hands over), and Mergify tests and merges (below), with no lock and no signing key, so a cloud session lands too (where its proxy refuses the status write, `land` comments a `Queued for Mergify` record naming the head instead, and the repo needs `.github/workflows/land-status.yml` to post `land` from it); the repo's `.mergify.yml` is `approvals.py queue-config`'s render of this skill's `mergify.yml`: the template with the `land` batch keys above and the `checks_timeout` `timeouts.run` sets (`gatekeeper.py timeouts --mergify` prints the same file), and `gatekeeper.py timeouts --check` (exit 1, one line) or `check setup` says when its `checks_timeout` is out of step (read it when setting the queue up: its comments say what each key does), which retries a red batch once, then splits it until only the failing PR leaves (`batch_max_failure_resolution_attempts: null`), and tests every batch on a draft PR from `mergify/merge-queue/`; a repo runs its tests once per tree by giving its required check's job a test step with the step-level `if: startsWith(github.head_ref, 'mergify/merge-queue/')`, never a job-level `if`, since a skipped required check never reports, so the PR head's run does only the cheap checks and still reports success, and `land` reads nothing more than that green check on the head; in a repo with a merge queue and free CI minutes, gate in the queue only instead (the header of mergify.yml lays it out): the gate's job gets that `if` at job level, so it runs only on `mergify/merge-queue/` heads and a PR head shows it skipped, the base's ruleset drops the gate from its required checks (a skipped required check never reports, so Mergify would never queue the PR), and the queue rule adds `merge_conditions: [check-success = <gate>]`, which differs from `queue_conditions`, so Mergify always tests on a draft PR and never in place; its undo adds the required check back, drops the job-level `if` and drops `merge_conditions`; keep the step-level variant in a repo that pays for CI minutes or whose queue rejects often, since there a PR head's run catches what the queue would reject; either way the supervisor sends a dequeue back to the lead that owns the PR, with its failing check, job and tests, and counts the queue's rejections; `approve merge` and `record verdict` each end by that hand-over when `check merge` passes (else they print what is missing and queue nothing), and `check stalled` lists each open PR with its go and a satisfied verdict that is neither queued nor merged (exit 1), so a land that was stopped or died is noticed, and also each verified PR that has waited past `loop.stalled_after_hours` for a go or record it lacks, naming what is missing (on any base: one `land` merges itself is listed while still open, one GitHub's merge queue holds while not in it, and a PR it can't judge from this checkout, such as one stacked on another PR's branch, gets its own `not judged` line and stops no other, and leaves the exit as it is: exit 0 with `not judged` lines means those PRs were not judged, not that none is stalled; a checkout's own edit of the policy files changes nothing, since the base's are read); with `signed_squash_to` it is refused, since GitHub signs Mergify's merges; `queue: github` records that the base uses GitHub's own merge queue: where the base's rules carry a `merge_queue` rule `land` hands PRs to it exactly as without the setting, and where they carry none `land` and `check stalled` refuse it, naming the missing rule, since `land` would then merge each PR itself while the policy says a queue does | GitHub merges, on a host, or in a cloud session on a base whose ruleset requires the merge queue; no wait for `batch_wait_seconds`; a PR whose head matches `merge_commit_heads` lands with `gh pr merge --merge --match-head-commit`, keeping the head's ancestry (an upstream-sync PR needs it), and every other PR is squashed as before; `land` prints each PR's method, and a match with `signed_squash_to` set is refused, since a signed squash has no merge-commit form |
| `timeouts` | `test` and `run`, each a duration of days, hours, minutes and seconds in that order (`90s`, `60m`, `4h15m`); a malformed one is refused, naming the key | every time limit of a run, from one place, so raising one moves the others with it: `test` is one test's, which the project maps onto its runner's own per-test option in loop.md's `Affected tests:` command (no runner is named here); `run` is one selection run's, the queue's check included. Derived from `run`: the Mergify queue's `checks_timeout` (`.mergify.yml`, rendered as above), a CI job's `timeout-minutes` (`run` in whole minutes, for the templates that render workflows) and `land --wait`'s ceiling, the queue's checks timeout plus a margin of one more run (the batch ahead of the PR, or the one retry of a red batch). `affected-tests` stops a run still going at `run` and exits 124 with one line naming the budget, the selection's size where it knows it (the whole suite under `Gate: full`; else unknown, with the count of changed files) and that it ran unsharded; a timeout is never a test's failure: a GitHub queue removal red only by `TIMED_OUT` checks counts for nothing, and a Mergify dequeue its own queue check run, red, calls a timeout is queued again, evicting no one (a refused queue command leaves `loop:dequeued` for the next land). `gatekeeper.py timeouts [--json]` prints every derived limit, and `timeouts --check` reports each one out of step (#2445): a workflow job whose `timeout-minutes` carries the comment `# timeouts.run`, the variable loop.md's `Wall-clock variable: <NAME>` line names (seconds), a hand-set `land.wait`. `affected-tests` estimates a run it can list (the whole suite) from `.qualify/durations.json` (`GATE_DURATIONS`; the nightly writes it with `durations --record`, the file `shards --durations` reads): recorded seconds over `GATE_WORKERS`, plus `GATE_SETUP_SECONDS`, the longest shard's under `select.shards`; over 80% of `run` it exits 124 in one line naming the estimate, the budget and the cause, with no durations it says so and runs. Each run appends its estimate and actual time to `.qualify/gate-runs.jsonl`, and `durations --report` flags drift over 25% | `test: 60s`, `run: 60m`: a 1 hour queue timeout, 60-minute jobs and a 2 hour `land --wait` |
| `carry_over_main` | `except`, a list of globs, required | deprecated, goes in the next release (Approvals and proof); the project's merge-check configuration (lint, type, test and gate config, lockfiles): clean merges of main whose changes touch none of the PR's files, where neither the PR's changes nor main's touch one of these, keeps the head's verdict, never its `local-ci` record (Approvals and proof). A glob matches the full path, as `paths` does: a nested copy needs `**/<name>` beside `<name>` | every merge of main needs its own verifier pass |
| `breakglass` | `max_age` (`7d` or `12h`), `max_commits`, `smoke` (a command, as `gate`), `earlier_runs`, `artifact`, all optional | the windows of the owner's break-glass proof (breakglass.md); `loop.owner` must name the owner's login, since only their status and record count | `7d`, `20`, the `gate`, `2`, `failed-tests` |
| `adapters` | `tracker`, `forge`, `evidence` | the adapter each of the gatekeeper's ports runs on, by name: `github`, `gatekeeper.py`'s own (`github_adapter.py`), is the one it ships, and a name no adapter has is refused, naming it (`gatekeeper.py`'s docstring) | `github` |

issue-tracker.md holds its label and board facts in a block of the same form under its own
`## Settings`, read by `approvals.py` (`label_names`), `labels.py` and `board.py`, refused the
same way; its Never on GitHub stays prose. Each section's values are strings:

```yaml
components:
  # a component, bare or in full: its label's description
  billing: invoices and payment runs.

categories:
  ops: runbooks and alerts.

labels:
  customer-reported: filed from a support ticket.

renames:
  # old label: new label
  frontend: area:web

board:
  # loop column: the board's column
  Build: In progress
```

| Section | Key: value | Without it |
|---|---|---|
| `components` | a component (`billing` or `area:billing`): its description | no `area:` label |
| `categories` | an extra category (`ops` or `kind:ops`): its description | the loop's categories only |
| `labels` | any other label, in full: its description | none |
| `renames` | an old label name: its new one (issues.md, Labels) | the loop's own renames only |
| `board` | a loop column: the board's column (github.md, Board) | each column keeps its own name |

## Independent rerun

A record (`loop-gate/<tree>-<key>`) proves a run of a command on a tree, not who ran it: workers share one forge
account, so a record branch pushed by hand counts for as much as one the gatekeeper pushed. For a change rated at a
level with `rerun: true` (by default `high` and `critical`), a red the nightly finds is found too late, so its merge
needs its selection run again where the worker has no control. The rule in inputs and results:

- **Input:** the head's tree, and origin's records on it made by `local-ci --record --rerun`, which runs the
  `Affected tests:` selection against `--base`, else the merge base (the full gate without that line), reusing no
  pass, and names `LOOP_RERUN` in its command and `Env:` line, so its record is keyed apart from the worker's and a
  branch of that name already on origin makes it fail rather than reuse it.
- **Result:** the merge passes when one such record either was pushed by the CI's app (the forge's newest push to
  its branch set it to that commit, from the app account `loop.rerun_pusher` names, by default
  `github-actions[bot]`, the workflow token), or is named by its commit on a `Rerun: <commit>` line of a verifier
  pass on the head, or on a head of the same tree that carries its verdict (a fresh session the verifier started,
  in a repo with no CI, pushed it; `loop.py brief` tells the verifier so there, and `report` warns on a high-risk
  report there that names none). Otherwise `check merge` and `land` refuse, naming what is missing. A level with
  `rerun: false` keeps the worker's record as the proof, and so does a PR that owes no verifier pass: an exact
  revert, a bot's PR and an incubating PR that waives the verifier.

In a repo with CI the rerun is two jobs, so the PR's code never runs with a token that can write. The first, with a
read-only token, runs `check rerun <pr>` (`rerun: needed`, `done` or `not needed (<why>)`, by the rule `check merge` applies, the
one step on a head that needs none),
then, where needed, `local-ci --record --rerun --no-push`, which makes the record commit, keeps it as a local branch
and names it, and hands it on as a git bundle. The second, holding the write token, needs the first's success,
checks out and runs nothing of the PR, checks that the bundle holds one branch, `loop-gate/<the head's tree>-<key>`,
whose commit holds the head's tree with the head as its one parent, and pushes it; on a fork's PR, which gets no
write token, it says so in one line and pushes nothing. Its triggers include the PR's labels and reviews, since a
`risk:high` label or a verifier's `Risk: high` can raise the rating after the push.

What it does not stop: in a repo with CI, the PR's own code decides the first job's result and the record it hands
on, so a PR whose code fakes a green run still gets its record pushed; what the split stops is that code pushing
anything else, or a record of another tree, with the write token. A workflow file a same-repo PR edits runs in place
of the base's, so such a PR can change both jobs; the verifier reads that diff, and a project whose CI pushes records
keeps its workflows under `risk.paths`. Only one account's push counts, `loop.rerun_pusher`'s: a repo whose
workers push with that same token (an agent run inside the CI, whose token is the workflow's) gets no independence
from this route, since a worker's hand-pushed record is then that account's push too; name another app there, or
leave such a repo on the verifier's `Rerun:` line. With no CI, a verifier pass is a review by the shared
account, so the gatekeeper trusts the verifier's `Rerun:` line as it trusts its `SATISFIED:` line. Signed attestations
are #2143's Not now.
