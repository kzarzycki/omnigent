#!/usr/bin/env -S uv run -q --script
# /// script
# requires-python = ">=3.9"
# dependencies = ["PyYAML==6.0.3"]
# ///
"""Approvals (engineering-loop): every step leaves proof on GitHub, and this checks it before the loop moves on.

    uv run scripts/approvals.py check <build|merge> [pr]  # one line per missing proof, exit 1; waiting: exit 3
    uv run scripts/approvals.py check build --issue <n>   # before the PR exists: the issue's build proofs; exit 0 or 1
    uv run scripts/approvals.py check build <n>           # <n> no PR has: the same as --issue <n>
    uv run --python '>=3.11' scripts/approvals.py check setup               # a pack upgrade's gate: the repo's policy parses and its mise tasks and workflows run the scripts with uv; exit 0 or 1
    uv run scripts/approvals.py check ready <pr>                # the author's step before the verifier: the PR body's `## Evidence` (and demo form) alone, the check merge runs; exit 0 or 1
    uv run scripts/approvals.py check stalled              # every open PR land would take that its queue does not hold, or verified and idle past loop.stalled_after_hours with no go or record: one line each, exit 1; one not judged here: its line, which changes no exit
    uv run scripts/approvals.py land [<pr>... [--only]] [--wait]  # land every landable PR through the base's merge queue; exit 0, 1 or 3
    uv run scripts/approvals.py approve <spec|plan> <issue> --by <owner|delegate>   # on the owner's word, or the delegate's under `loop.delegate: all`
    uv run scripts/approvals.py approve merge <pr> --head <sha> --by <owner|delegate> [--withdraw]  # a go on that head
    uv run scripts/approvals.py delegate <pr>              # the delegated approver's approved:merge, feature branch only
    uv run scripts/approvals.py verdict <pr> <report>      # post a verifier report as a PR review
    uv run scripts/approvals.py report <report>            # check a verifier report as verdict reads it; posts nothing
    uv run scripts/approvals.py overlap <pr>|--files <path>... [--base <b>]  # other open PRs changing the same files: hunks, and whether the heads merge
    uv run scripts/approvals.py local-ci <pr> [--head <sha>] [--run]  # --head: the commit just pushed, refused if GitHub's head stays another; with `ci: false`: post the head's recorded pass, refuse without one; --run runs `mise run check:all` here then
    uv run scripts/approvals.py local-ci --record [--base <rev>]  # the same on HEAD, the gate before each push; reused by tree; narrowed under `ci: false` or with --base (Narrowed mode)
    uv run scripts/approvals.py local-ci --status <head>   # the detached run on a head: running (exit 3), passed, failed
    uv run scripts/approvals.py local-ci --record --head-proof  # the same under the caller's env values: loop.md's head proof
    uv run scripts/approvals.py local-ci --record --rerun [--base <rev>] [--no-push]  # an independent rerun of HEAD's selection: nothing reused, its record keyed apart (levels' `rerun`); --no-push keeps the record commit as a local branch and names it, for a CI job without a write token
    uv run scripts/approvals.py check rerun <pr>           # whether the PR's rating asks a rerun and has one: `rerun: needed|done|no`, exit 0
    uv run scripts/approvals.py gate-record <head> [-- <command>]  # the verifier's read: origin's pass record of the gate (or <command>) on <head>'s tree, exit 0, else 1 naming what to run
    uv run scripts/approvals.py queue-select [<base>]      # a merge queue's batch check (R6): one command per line to run on the batch, the select.unkeyed checks and `affected-tests --base <point>` for each PR with no record; exit 1 when that is nothing while a PR has no record
    uv run scripts/approvals.py queue-config               # the Mergify config `.mergify.yml` holds: the pack's mergify.yml with gatekeeper.md's `land` batch_size and batch_wait_seconds
    uv run scripts/approvals.py timeouts [--json|--mergify|--check]  # every limit the working tree's `timeouts` derive; --mergify prints .mergify.yml from it, as queue-config does; --check: exit 1, one line each, when .mergify.yml, a marked workflow job's `timeout-minutes`, loop.md's `Wall-clock variable:`, a hand-set `land.wait` or the nightly's push paths (against `select.build_inputs`) is out of step
    uv run scripts/approvals.py durations [--record <junit.xml|durations.txt> [--out <file>] [--setup <seconds>]] [--report]  # the nightly's per-test durations file (what `shards --durations` and `affected-tests`' estimate read); --report: a line per gate run whose time is over 25% from its estimate
    uv run scripts/approvals.py affected-tests [--base <rev>] [--record]  # the worker's test run on a head: loop.md's `Affected tests:` command against <rev>, else the merge base; the narrowest set when it can't tell; `check` under `Gate: full` or with no selector; --record: its pass pushed as `local-ci --record` pushes a narrowed one; stopped at `timeouts.run`, exit 124 and one line
    uv run scripts/approvals.py rerun-tests <failed-tests.json> --red <head> [--base <rev>]  # a red run's one rerun (R5): the failed tests plus what the fix reaches, else the red run's whole selection, where a pass is a flake; the red head's `Affected tests:` line, never the base's; exit 75 when the rerun ran no test
    uv run scripts/approvals.py shards --suite <file> [--selection <file>] [--whole] [--durations <file>] [--passed <head> | --record <head> --shard <i>] [--command <cmd>]  # a whole-suite selection split into `select.shards` disjoint shards; --passed: whether each shard has a pass on <head>'s tree; --record: run shard <i> and record its pass
    uv run scripts/approvals.py release <issue>            # parked or dropped: remove the assignee the loop added
    uv run scripts/approvals.py graduate <epic> --pr <n>   # graduate an incubating epic: label off, then close
    uv run scripts/approvals.py heavy <command> [args...]  # one heavy command under the host's heavy wrapper; its exit code passes through
    uv run scripts/approvals.py heavy --plan               # the host's gate slot count and the limit that set it

gatekeeper.py is the gatekeeper; approvals.py, its name before #614, runs it for one release line, every verb above
unchanged. Its own names for three of them, and one verb more:

    uv run scripts/gatekeeper.py record approval <spec|plan|merge> <n> --by ...  # `approve`, the same arguments
    uv run scripts/gatekeeper.py record verdict <pr> <report>   # `verdict`
    uv run scripts/gatekeeper.py record checks [<pr>] [--record|--run|--status <head>|--head-proof]  # `local-ci`
    uv run scripts/gatekeeper.py report --json <report>   # the report's facts as JSON: verifier, head, verdict, ...

The policy reaches the tracker, the forge and the evidence only through their ports (PORTS), each run by the adapter
the policy's `adapters` section names for it (`tracker`, `forge`, `evidence`), `github` for one it doesn't:
github_adapter.py, through `gh`, with its GraphQL reads falling back to REST where GitHub refuses GraphQL, and every
posted body checked against the deny list first. A name no adapter has is refused, naming it. The gate's own commit
status counts under `loop:approvals`, the name the project template's workflow posts, and under `gatekeeper` for one
release line: `check merge` and `land` wait for neither.

At start every verb warns on stderr, once, when this script's checkout is behind its upstream's last fetched tip (no
fetch) and the script differs there: the count and `git pull`. loop.md's `pack_staleness: refuse` under `loop` makes
it a refusal (exit 1).

Run anywhere in the checkout: it reads docs/agents/ at the checkout's root. The policy is gatekeeper.md's block under
`## Policy`; without that file, loop.md's block under `## Settings`, with a deprecation line on stderr, and with both a
policy in each is refused, naming both, since two sources of the bar would drift. "gatekeeper.md's" setting below means the
policy's, in whichever file holds it. The project's `mise run loop:approvals
<point> [pr]` task runs `check`, and its `loop:land <pr>` task, where it has one, runs `land`; the loop and CI call
those tasks.

`check build` needs every issue the PR closes to be free of `needs-owner`, with exactly one category, a component and
exactly one size (a `wayfinder:` ticket needs only `approved:spec` and no `needs-owner`), and its spec approved: the
owner's current record and `approved:spec`, unless gatekeeper.md's `spec.auto` covers it
(below). A `## Plan` comment needs the same where the `plan` rule asks, and is due when gatekeeper.md's `practice` names a
`plan` skill. Category, component and size names are the loop's fixed set plus docs/agents/issue-tracker.md's
(`label_names`): a category is `kind:<name>`, a component `area:<name>`, whether the tracker file names it bare or in
full.
`check build` with a PR also runs `check merge`'s keyword check (below), where the lead hands the PR over, so a wrong
keyword is fixed before the PR reaches the landing queue. `check build` also refuses an issue assigned to anyone but its approver (the person who last added `approved:spec`,
or for a spec the policy approves the account running this), since an assignee says whose the issue is; and, with a
PR, an issue another open PR closes too (a closing keyword, or `Implements` under a `signoff` rule), naming that PR
and its branch, since each build runs its own verifier, gate and queue. When every build proof holds, it assigns each issue no one holds to its approver, comments a record that the loop did, and gives
the PR its issues' assignees, reporting a refusal and carrying on. A flow that releases the claim (the loop's `release`) removes an assignee that record names,
unless someone assigned them again after it, and keeps every other: a claim a person made stays.
Under Omnigent (`OMNIGENT_RUNNER_PRIMARY_SESSION_ID` and `RUNNER_SERVER_URL` set) the record's details add `Session:
<server>/c/<lead's session id>`, which names the lead from inside its workers and verifiers too, and the issue's `Session`
issue field is set to the same link. The field is optional: the account's owner creates it (a text field); without it,
the run says so in one line after one lookup, the record still carries the link, and the claim and the exit code are
unchanged. A refused write is a line too. Where GitHub refuses GraphQL, REST sets it, with the field's id read off an
issue already carrying a value. `check setup` prints one line and GitHub's steps when the account lacks the field.
A repo without docs/agents/gatekeeper.md or loop.md has no rules: each issue needs only `approved:spec` and no `needs-owner`, with no
approval record. Without docs/agents/issue-tracker.md no label's shape is checked.
`check merge` also fails a PR that itself carries `needs-owner` (`#<n> waits for the owner (needs-owner)`), so `land`
never takes it, alone or in a batch, until the owner's go on its head (`approve merge --by owner`) removes the label.
`check merge` adds the PR's, whose absence fails (exit 1): the `## Evidence` section of its body; the keyword linking
each issue, where gatekeeper.md has a `signoff` rule (below); the verifier's PR
reviews, a review counting only when its body starts `Verifier (<family>), pass <n>` (same-family included) and the
PR's author, the account running this or an account that can write to the repo posted it, since anyone can review a
public repo: every one on the newest
pass's commit says `SATISFIED: yes` with 0 blocker and 0 major, and that commit is the head itself, since every
commit after a verdict needs one of its own; on a high-risk change (below), that pass came from the default tier, not
one whose heading names `mid` or `small` (a `Verifier model:` tier, the verifier role), since a lower tier's review is enough
only for a change it rates low (a fallback verifier's family, `agy`, `gemini` or `same-family`, is no tier), and on a PR
that only bumps the pack's pin, the pin target's tier rule judges that pass instead (pin_target); no unresolved
review thread; no review whose latest state is `CHANGES_REQUESTED`; and a newest `## Demo` comment, where a trusted
author posted one, that embeds its videos and screenshots as user-attachments (`demo.sh`), which `check build` checks too. Then it waits (exit 3, one line per wait; the approvals workflow maps it to a `pending`
status) for the newest run of each check the base branch requires, as `land` reads them, to be green on the head,
since GitHub merges nothing without them (a base that requires none waits for no check, and `ci: false` asks no
`local-ci` pass: the verifier's record is the merge's proof, and no gate runs after it), and, while the change's level asks a go of its merge
(below), for the owner's go on that head: the newest commit status `owner` on the head commit by the account running this or a writer is
green, and the PR holds the owner's record (`Approved by the owner.`, then `Approved: merge`, `By: owner`,
`Head: <sha>`) naming that head. `approve merge <pr> --head <sha> --by owner` posts both on the owner's word, the
record's every word the script writes, refusing a head other than the one named, which is the one the owner was
shown; `--withdraw` posts a newer failing status and a `Withdrawn: merge` record and removes the `approved:merge`
label. A status belongs to one commit, so no push carries the go to a head the owner didn't see. `--by delegate` is
the delegated approver's go, pinned to one head the same way: the `owner` status described as the delegate's (`The
delegate's go on <sha>, under the owner's delegation`) and the record `Approved under the owner's delegation.`, then
`Approved: merge`, `By: delegate`, `Head: <sha>`, so the PR shows whose go it is. Where GitHub refuses the status
write with a 403, as a cloud session's proxy does (#1816), the record is posted with a `Status: refused` line and
stands as the go alone on a head with no `owner` status: the newest such record, or `Withdrawn: merge` record, naming
that head and written by the account running this or a writer decides, still pinned to its head; over a status on
the head only such a record written after it decides (so a withdrawal recorded from the cloud takes back a host's
go), and a record without the line still needs its status. Into the default branch it is
refused unless loop.md's `delegate: all` under `loop` (default `feature`) lets the delegate's go cover that branch
too, and a delegate's status there counts only while it does, so removing the setting withdraws those goes on open
PRs; into a feature branch it counts either way. `check merge` and `land` print whose go, the owner's or the
delegate's, a head whose level asks a go has. Into the default branch these are the only go: a
label is anyone's with triage rights, the delegated approver's included, so `approved:merge` there is no go, nor is
an `owner` status an older gate wrote from the label (marked as from it). On a change whose level asks no go the
label means nothing. Risk is a rating, and the policy's `levels` say what each level asks (rating, levels): `high` is
`risk:high` on the PR or an issue it names, or a risk rule matching (below); `critical` a path `risk.critical_paths` names
(below); `low` anything else. Each level asks `policy` or `owner` of the spec and `gates`, `go` (a person's go on the
head, as above) or `owner` (the owner's own go on the head, into any branch, no delegate's go nor label counting) of
the merge; the merge takes the strictest any of its terms asks. The defaults are `low` {policy, gates}, `high`
{policy, go}, or under `spec.auto: true` {owner, gates}, and `critical` {policy, owner}. A term the spec's hold never
reads (the label, a revert, the `verifier` term) rates the change only after its spec was approved, and asks
`go` where its level asks `owner` of the spec and `gates` of the merge, since the owner never saw that rating.
`check merge` and `land` print the level and the merge action on stderr (`PR #7: risk: high (<why>); merge: go`). When
the gate rates a change high or critical, `check merge` and `land` add `risk:high` to the PR, reporting a failure to
add it and carrying on, except for a term the label, read back as a `high` rating after the spec, would ask more of:
by default a rule that held the spec under `spec.auto: true`, whose level is {owner, gates}. On a PR into a feature branch (any base but the repository's default branch)
`approved:merge` added after the head was pushed is a go too, the push time being GitHub's repository activity for
the head branch; a label added before the push is no go, and the approvals workflow removes it on `synchronize`. There
the label may be the delegated approver's: `delegate <pr>` adds it, then a record (`Approved: merge`,
`By: delegate`), and refuses a PR into the default branch, where a label is no go. A PR from a branch
into the default branch is high risk while any PR merged into that branch carries `risk:high`, so a feature
branch's merge waits for the owner.
Every proof held: exit 0. Before the PR exists, `check build` judges the issue `--issue <n>` names (or a positional
number no PR has), else the one
the branch's name starts a segment with (`loop/329-fix`), the same way, and claims nothing; with neither it exits 1,
since a build needs an approved spec. `check merge` with no PR yet: exit 0.
With `opt_out: true` in gatekeeper.md, a person may keep their PRs out of the loop: `check merge` on a PR that
left no loop trail (no verifier review, no `Approved: spec` record or `approved:spec` on an issue it links, no
`Reverts #<n>` line, not an exempt bot PR) prints that it is outside the loop and exits 0, so the approvals
workflow posts success and its author merges it their usual way; `land` never takes it into a batch, and named,
prints the same line and exits 1, so the loop never merges it. Risk rules and `risk:high` hold no such PR. `check build` is
unchanged. A spec a policy covers is no trail, since under `spec.auto: true` every issue is covered.
`land [<pr>... [--only]]` is the one way an agent merges. It lands every landable PR into one base, the first named PR's or else the
repository's default branch, through one of three merge queues, picked by the base's rules (`required_checks`) and the
policy: the GitHub merge queue where the rules carry a `merge_queue` rule, else the Mergify queue where the policy's
`land.queue` is `mergify` (below), else the local merge queue, the path for a project without a queue (held_by).
`land.queue: github` records the project's choice of GitHub's queue: it changes nothing where the rules carry the
`merge_queue` rule, and is refused (exit 1) where they carry none, since `land` would then merge each PR itself while
the policy says a queue does. All take the
same candidates (step 2). A queue gates each group it merges in CI (GitHub's `merge_group` run, Mergify's batch
check); the local merge queue runs no gate at all: a PR merges on the verifier's record on its head, with the checks
the base requires green there, and nothing reruns at land (#1872). Since no queue run tests that merge, a head with no
record on origin of loop.md's `Affected tests:` run against its merge base (`gate-record <head> -- <the narrowed
command>` exit 1; the gate's record where the project has no narrowed run) is not landed, and `land` says which record
is missing and that `affected-tests --record` (else `local-ci --record`) on that head makes it (head_recorded, R6); a
break-glass PR, which the owner merges, is not held. One `land` runs one of them, never a mix. The local
merge queue lands its candidates as one batch:
1. It takes the landing lock, the branch `loop-landing/<base>` on `origin`, which only moves forward, so a host whose
pushes may only create or fast-forward a branch under `refs/heads/` (a cloud session's) takes and releases it too. Its
tip says the state: each lock commit holds the empty tree, its title says `landing lock: held` or `landing lock: free`
with `[skip ci]` (so a project whose workflows run on every branch push starts none), its body the pid, the PR
it lands (`land <pr>`'s, none for a bare `land`) and a random value (so two lands in the same second never make the same
commit), and its commit date is its age. A land pushes a `held` child of a `free` tip (or the first lock commit, with no
parent, where the branch is missing), leased on that tip with `--force-with-lease`, so of two lands from one tip one
wins. Once its batch is merged locally, before its first merge on GitHub, it moves the lock to a `held` child of its own naming the
batch's last PR, leased on its own, so a lock taken by another land meanwhile stops it, merging nothing (exit 1). A
`held` tip is another land's: exit 3, naming its holder and age. One naming a PR that GitHub reports merged, with its
merge commit in origin's base, is released, left by a land that died after its merges: it is broken and taken in the
same push, a `held` child of it. One naming no PR (a bare `land` before its merges, or a lock written before #901), an
open one, or one GitHub can't read now waits; one older than 2 hours is stale, left by a land that died before its
merges, and is broken the same way. The lock is released at the end whatever happened, a SIGTERM or SIGHUP included (exit 128
plus the signal), by pushing a `free` child of ours leased on it, so a stale one broken and taken by another land stays
theirs; SIGKILL leaves it to be broken as stale. No push deletes a ref or rewrites one, so the branch's history only
grows. While the previous version's lock `refs/loop/landing` is on `origin` (read, never written), `land` waits on it
(exit 3), so a host still on that version and this one never land at once; one older than 2 hours is stale, passed
over and left in place. The base is fetched once the lock is taken, so the batch starts from the base as it is then.
2. The candidates, oldest PR first, are every open PR into the base from this repository, not a draft, that holds every
proof of `check merge` but its CI wait, is not outside the loop (`opt_out`, below), has the owner's go where it is high
risk, and has the checks the base requires on GitHub (`required_checks`, but `loop:approvals`, which is check merge's
own output) green on its head. Under `ci: false` the head needs no `local-ci` pass. With loop.md's `head_proof`, the head also carries its head proof (below), else it is no candidate and `land`
names the command that records it. `land <pr>` on a draft marks it ready, never on a missing proof, since ready starts CI, and does so before it
waits for the lock, so the holder's next batch includes it; while the checks that starts are required, it waits (exit 3).
`land` takes several PRs: each named draft is marked ready the same way, and the named PRs land in one batch with the other
candidates. `land <pr>... --only` (with or without `--wait`) makes the named PRs the only candidates: every other PR is left untouched (not
marked ready, commented on, evicted or merged), and the rest is as without the flag; `--only` without a PR exits 2.
The check of each PR (`landable`) writes nothing to GitHub: `land`'s own path marks a ready draft ready and adds `risk:high`, so a probe that only reads never readies a draft.
A named PR that is no candidate (or is closed, or targets another base than the first's) is left out and says why, and the rest still land: a missing proof exits 1,
a wait for the owner or a pending required check 3, a red required check 1; with several PRs named the exit is the worst of them, 1 over 3 over 0.
A named PR missing a required check, or a named draft, that GitHub reports conflicting exits 1 with the fix, `merge
origin/<base> in` (a draft is not marked ready first), since GitHub runs no workflow on a conflicting PR, so the check
would never come.
3. With loop.md's `land: batch_wait_seconds` set (a whole number, default 0: no wait), and before the worktree below,
it waits up to that long while another open PR into the same base is held by nothing but its required checks still
running (its proofs and any owner go already hold: a red check, a draft, a proof gap or an owner wait never counts),
polling every QUEUE_POLL seconds; each such PR that turns landable joins the batch. It stops
when none is pending or the time is up, so a lone PR lands at once; with PRs named by `--only` it never reads or waits
on other PRs. It prints the wait and who joined, and the `Landed` comment's `Batch:` line adds `(waited N s, joined:
...)`. The key is optional in `land`, which asks for signing keys only where `signed_squash_to` is set.
   In a temporary worktree of `origin/<base>` under the git common dir, removed after, it first runs loop.md's
`pre_land`, where set, for each candidate: a command parsed as `gate` is, run there at the base's tip with `LAND_HEAD`
(the PR's head) and `LAND_BASE` (that tip) set, so the base's tree holds the check and a PR never changes what judges
it. A non-zero exit leaves that PR out of this batch, printing the check's last lines on every land, with a comment,
once per head, holding its output; it is not red, so it is neither evicted nor turned back to draft. Then it merges each candidate's head
(`git merge --no-ff`). A PR that conflicts is skipped, with a comment, once per head, saying it conflicts with the base
plus the PRs merged before it, so it needs the base merged in. No gate runs on the result: no `gate` or
`gate_steps` command, no reuse of a recorded pass, no re-run of a red one.
From a clone with no working tree (no index: a `--no-checkout` launch dir), where every command reads the policy files
from origin's default branch (policy_ref), step 3 makes no worktree: the heads merge in objects (`git merge-tree
--write-tree`), and `land` refuses a `pre_land` it has no checkout to run in.
   `check merge` runs the same `pre_land` the same way (one function, pre_land_run), in a temporary worktree of the
base's tip, once nothing else holds the merge, so it never calls clear a PR `land` would leave out: a refusal prints
`PR #<n>: `<cmd>` refuses it against <base> at <tip>, exiting <code>: land leaves it out` with its output's last
lines and exits WAITING, not 1, since land keeps the PR ready; it posts no comment. While something else holds the
merge it prints `PR #<n>: pre_land not run: `<cmd>` (land runs it once nothing else holds the merge)`, and from a
clone with no working tree, where land refuses a `pre_land`, a `pre_land not run` line saying so, exiting WAITING.
4. It merges each PR through GitHub in that order, pinned to its head (`gh pr merge --squash
--match-head-commit`), with the body the repo's squash default writes. A merge GitHub refuses, or leaves unmerged
(a merge queue the base's rules don't name, such as classic branch protection's, only queues it, to merge later on a
tree no land saw), stops there: that PR and those after it wait for the next land. Then it deletes the record
branches no head needs: those of the heads it landed, and those over a day old whose tree is no open PR's head (a head
replaced or never landed; the day spares a record pushed before its PR opened). A refused delete is said and never
fails the landing. No gate runs on the base's new tip either: the nightly's full run is what catches a break the
merged trees make. Each PR landed gets one comment, `Landed`, then `Base:` the new tip, `Tree:` the tree it landed
(the base with it and the PRs before it merged), `Gate: none at land, on the verifier's record of the head`, and
`Batch:` the PRs landed together. Each PR it merged then has its kept `--pr` worker sessions finished by a detached
(the omnigent-agents skill's `omnigent_agent.py reap --merged --pr <n>`), whose outcome goes to its own log, never the land's.
Exit 0 when the named PR merged (on a GitHub-queue base, also once it is enqueued or queued), or with no PR named when nothing landable failed; 1 for a refusal, a conflict, an
eviction (a queue's) or a merge GitHub refused; 3 while it waits; 143 or 129 when stopped by SIGTERM or SIGHUP.
`land --wait` runs it again while it waits on the lock or on CI, printing each new state once, for up to its ceiling,
derived from the policy's `timeouts.run` (the queue's checks timeout) plus a margin of one more run, for the batch
ahead of the PR or the one retry of a red batch: 2 hours with nothing set, 8 hours with `run: 4h` (`gatekeeper.py
timeouts` prints it; a `land.wait` duration such as `4h30m`, deprecated, still overrides it), then exits 3, leaving the PR's event `waiting` where a queue
or its checks still move it, else `needs-owner`; a wait for the owner ends it at once, since only the owner clears it. Between two runs it reads every 30
seconds only what it waits on, at no API quota: a held lock with one `git ls-remote` (running again once the lock moves
past its holder's heartbeat, or has not moved for 15 minutes), a named PR's CI with one ls-remote of the base and the
heads and an ETag read of each head's checks, which GitHub answers 304 for free while nothing changed (#1915).
Each named PR's branch carries the `land` event (events.md): `waiting` rewritten at each poll of `--wait`, `ok` once it
merged (its refs cleared EVENT_GRACE seconds later, so a `wait land <branch>` finds the end), `failed` when evicted (its
`url` the log comment), `needs-owner` at the owner's go or at the ceiling while nothing moves the PR. A write that fails changes no exit code.
The GitHub merge queue takes no landing lock (GitHub orders the entries, and two lands enqueueing one PR change
nothing), builds no local batch, runs no local gate, publishes no pass and deletes no record branch. Once it has fetched
the base (the base-drift refusal below), each of step 2's candidates, oldest PR first, goes in with GraphQL
`enqueuePullRequest`, `expectedHeadOid` the head the proofs judged, and GitHub drops an entry whose head moves, so no
proof outlives a push. Where GraphQL answers 403 it goes in with auto-merge on (REST `ccr/auto_merge`, the queue's
merge method and that head), and the PR is read again: a head other than the judged one has auto-merge taken off
and is refused (exit 1), naming both heads, and the other candidates still go in. A PR already queued is left alone, unless its entry is in `AWAITING_CHECKS` with no check suite
on its group's commit 10 minutes after that commit was made (GitHub lost its `merge_group` event): then it is dequeued
and enqueued again. Else
the queue's last removal of the head decides: the PR's `RemovedFromMergeQueueEvent`s since its head was pushed and since
it last went ready, so a PR evicted and marked ready again counts anew. `merged`: nothing to do (exit 0), since GitHub reports
the merge a moment after the removal. A PR GitHub reports conflicting: step 3's conflict comment, once per head, and not
enqueued (exit 1). A `merge_conflict` removal holds only that long, since the queue tests an entry on the entries ahead
of it, so the conflict may have been with one of them: while GitHub works the PR's mergeability out again after the
base moved, land exits 3 at once (`GitHub is still working out whether PR #<n> conflicts`), and a later land, once GitHub
reports it mergeable, enqueues it again. `failed_checks`: first the checks the base requires are read on its tip; one red there stops
the land (exit 1, `<base> itself is red`), counting nothing and evicting nobody. Then each such removal is placed on
the base commit its group was built on (the merge base of the base and the group's commit): a required check that
failed there (`FAILURE`, `TIMED_OUT`, `STARTUP_FAILURE` or `ERROR`; a run cancelled, stale or awaiting approval failed
nothing, and the queue's own run on that commit went green) makes it the base's fault, so it never counts and gets no comment, and the PR is enqueued again; still running,
nothing counts yet (exit 3, `<base>'s checks on <sha> are still running`). Of the removals that count, the head's first
gets a `Red in the merge queue` comment naming the check that failed on the queue's commit, its run and
its failed job log's last 30 lines, and is enqueued again; its second turns the PR back to draft with the same, as
Mergify's queue evicts (`Evicted from the merge queue`, exit 1). A `failed_checks` removal whose required checks on the
group's commit failed only by timing out (`TIMED_OUT`) counts for nothing, as any other removal (`checks_timed_out`,
`manual`) does, and the PR is enqueued again: a timeout is no test's failure (#2228), until the head's second one (a removal of either kind): that PR stays out of
the queue, `land` printing one line that names `timeouts.run` and its length and exiting 3 as a wait on a person (raise
the budget or shard the suite), and a Mergify dequeue its check run calls a timeout does the same on its second at one
head, from the `Timed out in the merge queue` records land comments (#2308). Either queue's stop lifts with a new
head, a ready-again (as its reds' count does) or a changed `timeouts.run`, which the `Timed out twice in the merge
queue` record each leaves at the head names (`Run:`). Mergify records its first timeout too, so a budget raised between
the two makes the next a fresh first; the GitHub queue records only its stop, so both timeouts count. The count lives in GitHub's timeline, so any land applies it and `land` keeps no
state. A PR enqueued or already queued exits 0, naming its queue position and head, and `land` ends. `land <pr> --wait`
waits on instead until the queue merges it (exit 0, after its `Landed` comment) or takes it out, whose removal the
next round reads as above (#2101): between rounds it reads, each QUEUE_WATCH seconds (60), only the PR's state, head and
queue entry, one GraphQL call (two ETag reads on REST, a 304 costing no quota), so a lead need not poll the PR itself;
the same holds under `land.queue: mergify`, where a merge, Mergify's dequeued label or a push ends that read. A later
`land <pr>` reads a removed entry's state once (above). Exit 3 stays for a PR whose conflict GitHub is still
working out, or a base still running its checks: nothing was enqueued yet. Without `--wait`, who tells the lead of a merge or a removal
is the project's watcher, not `land`. The first land that reads
a PR the queue merged (a `land <pr>` naming it, or a bare `land`, which reads every merged PR into the base that carries
`loop:queued`, the label `land` puts on each PR before it enqueues it) comments `Landed` on it, `Base:` its merge commit, `Tree:` that commit's tree and `Gate: merge
queue` with the links of the runs on that commit (the queue's `merge_group` CI), and finishes its worker sessions as
step 5 does, then takes the label off, so a trail that failed (said, never failing the land) is written by the next
land. The queue merges with its own `merge_method` and writes the squash message from the repo's squash
default, which `land` neither sets nor overrides, as in step 5. A head matching
`land.merge_commit_heads` is refused unless the queue's method is `MERGE`, since the queue merges every entry one way;
`land.signed_squash_to` is refused, since GitHub signs the queue's commit. A cloud session takes this path too, with
no `land.signing_key_env`, since GitHub signs: REST answers every step (auto-merge off dequeues; the PR's queue state
is its timeline's queue events, GitHub's `gh-readonly-queue/<base>/pr-<n>-<sha>` ref and the repo's `merge_group`
runs), and since REST names no removal's reason, each is read from its group's commit: `merged` when GitHub reports
the PR merged or that commit is in the base, `failed_checks` when a check the base requires failed on it,
`merge_conflict` when GitHub reports the PR conflicting or is still working that out, else none, which counts for nothing.
With `land.queue: mergify` in the policy (gatekeeper.md's `land:` section), on a base whose rules require no GitHub merge
queue (they win), a Mergify queue that `.mergify.yml` configures merges instead, which needs a repo no organisation owns
no GitHub queue for: `land` takes no lock, runs no gate and merges nothing. It judges the same candidates (step 2) with
the base's code, and hands each to Mergify by posting the `land` status (success, `Queued for Mergify at <sha>`) on its
head and adding `loop:queued`; `.mergify.yml`'s queue conditions
read both, and the status binds the hand-over to that head, so a push leaves them unmet. Where GitHub refuses the
status write with a 403 (a cloud session's proxy), `land` adds the label and comments a `Queued for Mergify` record
with `Head: <sha>`, once per head, and the repo's `.github/workflows/land-status.yml` posts the `land` success on that
sha for a writer's record while it is the PR's head and the label is on; an eviction's `land` error refused there is
said, and the label coming off leaves the queue. A PR already carrying the label
with `land` green on its head is reported queued and left alone. Mergify tests each batch as a draft PR on top of the
base in CI, retries a red one once itself and squash-merges on green; a PR it dequeues (red again, or conflicting) gets
its `dequeued_label`, `loop:dequeued`, and the next land that reads it with `land` green on the head evicts it: `land`
`error` on the head, both labels off, back to draft and an `Evicted from the merge queue` comment naming Mergify's
reason where its queue check run on the head gives one (a label left from an older head just comes off). A dequeue
whose queue check run's title or summary says it timed out is no test's failure: the label comes off and `@mergifyio
queue` takes the PR back, evicting no one (#2228). A bare land, or
`land <pr>` naming a merged one, comments `Landed` on each merged PR still carrying `loop:queued` (`Base:` its merge
commit, `Tree:` that commit's tree, `Gate: Mergify merge queue` with the Actions runs on that commit), finishes its
worker sessions and takes the label off. Exits as the GitHub merge queue path: 0 once queued, 1 for a refusal or an
eviction, 3 while a required check on the head runs (`--wait` waits for that, and for a named PR it queued, until
Mergify merges it or dequeues it, as above). Every call of a round is REST, so a cloud session lands this way with no signing key; with `land.signed_squash_to` it is refused, since GitHub signs
Mergify's merges.
With loop.md's `land` section (`signed_squash_to: <branch>`, `allowed_signers: <path>`), for a repo that trusts only
commits signed by its maintainers' keys (GitHub signs its own squash with GitHub's key), the batch is the same, but each
PR lands as a squash made in this checkout instead: one commit holding the tree it lands, on the base's tip the
batch was merged onto (or the squash before it), so each commit holds its own PR's change, titled `<PR title> (#<pr>)`, signed with the checkout's SSH key
(`gpg.format ssh`, `user.signingkey`) and checked with `git verify-commit` against the allowed signers file. It pushes
that commit to the PR's branch, pinned to the head the gates checked, then fast-forwards the base to it, so GitHub shows
the PR merged. A base that moved since the batch was merged is refused, since the squash would drop what landed
meanwhile; a base push refused puts the PR's branch back on its head. It is no candidate when it comes from another
repository (its branch isn't ours to push), targets a base other than the section's, has auto-merge or the merge queue
on (GitHub would sign that squash), or when `origin` isn't the PR's repo, the checkout signs nothing or the signers file
is missing. Without the section nothing changes.
`land.merge_commit_heads` (a list of fnmatch patterns over head branch names, such as `sync/*`) lands a PR whose head
matches with `gh pr merge --merge --match-head-commit`, a merge commit that keeps the head's ancestry (an upstream-sync
PR needs it), instead of the squash above; every other PR lands as before. `land` prints each PR's method, and a
match with `signed_squash_to` set is refused, since a signed squash has no merge-commit form.
`where: host | cloud | both` (default `both`) says where `land` runs: it refuses elsewhere, exit 1, before reading any
PR. A cloud session is one where Claude Code sets `CLAUDE_CODE_REMOTE=true`. There, without `signed_squash_to`, `land`
merges each PR over REST (`PUT pulls/N/merge`, squash, `sha` the judged head), since GitHub's GraphQL merge is refused by
the session's proxy; GitHub signs that commit as it does a host's, and no signing key is needed. With `signed_squash_to`
its checkout signs with the session's own key, shared across that service's users, so `land` refuses without
`ssh-keygen` installed, when it names no `signing_key_env`, and when that variable
is unset, empty, no unencrypted SSH private key, or a key whose public half is no line of the allowed signers file, each
refusal naming which and never the key. Else it writes the key to a mode-0600 file in a fresh temporary directory,
removed however the land ends, and every git command of the land runs with it (`-c gpg.format=ssh -c
user.signingkey=<file> -c gpg.ssh.program=ssh-keygen`, over the session's key and signing program), so each squash is
signed and verified with it. There REST alone answers, and it says neither whether a PR is in a merge queue nor
whether classic branch protection requires one, so a PR is a candidate there only with `merge_queue: false` in the
section, the project's word that its base requires none (a ruleset that does picks the GitHub merge queue, above);
a draft is marked ready and an evicted PR turned back to draft through the proxy's draft routes
(`ccr/ready_for_review`, `ccr/convert_to_draft`). Off a cloud session only `where` is new.
`land` (once it has fetched the base) and `check merge` refuse, naming the files, when this script or the tracker file, loop.md or gatekeeper.md differ from `origin/<base>`'s, so a PR is judged by the base's code and rules, never its own: run from a checkout of the base. `check merge` under `GITHUB_ACTIONS` also refuses when the checked-out commit is not the base's tip. `land --wait` that meets the refusal because another land moved those files while it waited fast-forwards a clean checkout of the base (its branch checked out, no tracked file edited) and runs again from the new code, keeping its ceiling; any other checkout is left alone and the land runs again from a fresh detached worktree of `origin/<base>`, removed when it ends; the refusal stands only when nothing newer exists to run from (the checkout is the base's tip, or the base lacks the running script).
loop.md's `carry_over_main` and `ci` work as below for one more release, each printing one `deprecated:` line on
stderr. The keys that drove a gate at land, `batch_queue` under `loop` and `land`'s `gate_on`,
`cloud_gate_timeout_seconds` and `host_gate`, do nothing (#1872): a policy that still sets one parses, with one
`ignored:` line on stderr naming it, and the `check queue` and `batch` verbs are gone.
`local-ci` runs only on a clean checkout at the PR head, and records nothing if the check fails or changes the tree.
In a shallow clone (a cloud session's), before the gate it fetches the base (the PR's, else the default branch) into
`refs/remotes/origin/<base>` and deepens (`git fetch --deepen 50`, up to three times, then `--unshallow`) until `git
merge-base origin/<base> HEAD` answers, so a gate step that diffs against the merge base gets a ref; each step's outcome
is one `local-ci:` line on stderr. A full clone runs nothing new, and no tracked file changes, so the recorded tree is
the same.
The check is the gate: loop.md's `gate` under `loop` (default `mise run check:all`), variables it sets in front of
its command included (`GATE_UI_TESTS=0 mise run check:all`), one command run with no shell, so an operator
such as `&&` is refused. It runs with every name loop.md's `local_ci_env` lists
removed from the environment unless the command sets it, so a host's ambient tier never changes what ran and the
command alone says what a pass proves. A pass also leaves a record keyed by tree and command under the git common dir
(`approvals/local-ci/<tree>-<key>.json`: head, tree, command, exit, env, time, host, seconds, slot_wait_seconds,
load_1m, swap_used_mb, `head` being the commit it ran on, `env` the variables the command sets, `<key>` a digest of
the command, `seconds` how long it ran, `slot_wait_seconds` how long it waited for the host gate slot, and `load_1m`
and `swap_used_mb` the host's 1-minute load average and swap in use, in MiB, when it started, each null where the host
doesn't say), which every worktree sees and nothing commits. With loop.md's `gate_steps` under `loop` (a list of `name`, `command`, parsed as `gate` is, and `where`: `any`,
or `host`, never run in a cloud session), in place of `gate`, the gate is those steps in order, and each step is a gate
of its own: its pass is recorded, published and reused by its tree and command alone, its record naming it as `step`,
so `local-ci`, `local-ci --record` and `land` run only the steps with no pass on the tree, stop at the first that fails
(a failed step records nothing, so it is never reused), and print one line per step naming the record branch or
commit each reused one passed on. A step may also name `paths`, a list of globs (fnmatch, case and all) of the files
it reads (#963): its pass is then found by its command and its inputs key, a digest of the `gate_steps` setting as
read and the mode, object id and path of each file under its paths in the tree, held as the record's `inputs` and its
record branch's `Inputs: <key>` line, so any tree with the same key reuses it, from this clone's records or from
origin's record branches of that command, and the step's line says `<step>: reused, inputs unchanged since <tree>`. A
found record counts only when today's `paths` give its own tree the same key, so one whose tree holds other inputs
counts for nothing. A step without `paths`, with an empty list, or whose paths match no file in the tree is keyed by
the tree, as above; a change to `gate_steps` changes every keyed step's key. A step may set `on_demand: true` (#2295),
for a check too costly to run on every PR (an hours-long run against a paid external system): `local-ci` and
`local-ci --record` skip it, each saying `<step>: skipped, on demand`, `land` reads only the other steps' records, and
the gate's pass never needs it, since a
gate step runs on every PR whose tree has no pass of it. `local-ci --record --step <name>` (repeatable, with
`--record` alone; a name `gate_steps` lacks is refused) runs those steps alone and records, publishes and reuses each
pass as any step's, by its inputs key, so an unchanged input never runs it again; it writes no `gate` status or event,
since it is no pass of the gate, and `gate-record <head> -- <its command>` reads its pass. A cloud session runs no `host` step: `local-ci --record` there runs the rest, says
which step it left and exits 0, with no head proof and no pass on the whole gate, which `local-ci <pr>` needs
and refuses there. Each step that runs, or the one gate, holds the host gate slot (an exclusive flock on
`${XDG_CACHE_HOME:-~/.cache}/engineering-loop/gate.<n>.lock`, n up to the host's slot count) for its run only, with `LOOP_GATE_SLOT_HELD` set to that
path so a wrapper in it takes no lock of its own, and none when this process already runs under it. The count is `GATE_SLOTS`
when it is a whole number from 1; else, where loop.md has the line `Gate cost: <cores> cores, <GB> GB` (optional
`, reserve <GB> GB`, by default a quarter of the host's total memory, at least 4 GB), the smaller of the cores ÷ the
cost's cores and (total memory − reserve) ÷ the cost's GB, at least 1; without the line, a third of the cores, at least
1. `GATE_SLOTS=off` takes no slot at all. `heavy --plan` prints the count and the limit that set it, `slots <n>:
<cores|memory|GATE_SLOTS|off> (<cores> cores, <GB> GB, cost <c> cores <m> GB, reserve <r> GB)`, as the template's
`gate-slot --plan` does, and a wait for a slot says the same line; `local-ci --record` makes one on HEAD with no PR. On a checkout with changes `--record` runs the check, records
nothing and exits with the check's code, so it is the gate while iterating too and its clean green run, the last one,
records itself.
Unless it already leads its own session, `local-ci` starts itself again in a new one, stdin from /dev/null, and follows
it, exiting with its exit code: a caller killed with its process group (a harness capping a background command) leaves
the check running, and it still records its pass or failure. Beside the records it writes `<head>.log` (its output),
`<head>.pid` (its pid and start time, so a pid the system has given another process reads as gone) and, once it
ends, `<head>.exit` (its exit code), keyed by the commit it started on. A `local-ci` on a head whose run is still going
follows that run instead of starting another (after it, one with other arguments runs its own), and `local-ci --status
<head>` says whether it is running (exit 3), passed (0, only when a pass on the head's tree is recorded) or failed (1,
naming the exit), with the log's path. The run's stderr goes to the same log, so a caller prints the check's stderr and
a refusal (`exited N: nothing recorded`) on its stdout. Stopping the caller, Ctrl-C included, leaves the run going:
`kill -- -<pid>`, with the pid `<head>.pid` holds, stops its process group.
A run that gates a branch also writes its `gate` event (events.md, `refs/loop/gate/<branch>`, the checked-out branch): `waiting` while a step waits for a slot or runs, rewritten every SLOT_SAY_SECONDS; `ok` at the end with
`url` the record (with `gate_steps`, the records' directory); `failed` with `url` the detached run's log when there is one,
else empty (a failed step records nothing, and a refusal has no log either). The detaching `local-ci` writes `waiting`
first, before it starts the run, so a wait that follows it never reads the previous head's end, and ends the event itself when the run wrote none (`failed` with the log on a nonzero exit, `ok` with no url on exit 0, as a record on a changed tree); a step left for a host keeps `waiting`. Writing an event never
changes the exit code. The `gate` commit status stays on the PR head: `wait-for --until gate` reads it and the PR
page shows it, and no event reader replaces it yet. `land` publishes none on main, since it runs no gate.
Every pass this clone holds, run or reused, is published one way from every session, host or cloud: pushed to origin
as the branch `loop-gate/<tree>-<command's key>` (a branch, since a cloud session's proxy pushes nothing else), on a
commit holding the tree, the commit the pass ran on as its parent, and as its message the line `Tree <tree> on
<commit's first 12>: <command>` (the command's digest, `sha256 <key>`, when it doesn't fit GitHub's 140 characters)
and an `Env:` line, then `Seconds: <n>` and `Slot wait seconds: <n>` (a record pushed before them still counts); `local-ci` says which branch. origin refusing the push (a commit not pushed yet) or unreachable is
said and changes nothing else: the record stays local, and a later `local-ci` publishes it. The pass is then shown as
a commit status on the head, success, context `gate`, description that line, where the token allows; a refused status
is said and is no error, since no decision reads it: anyone who can write a status may type one. A pass is reusable
for a commit when this clone's record for its tree holds exit 0 under today's command, or else when origin holds that
tree's record branch under today's command whose commit says that tree, command and env: a branch in the base repo is
its write access, so one on a PR's fork never counts, and it counts at every risk. A clone that reuses a record writes
the `gate` status it holds on the head, so the PR shows the pass. So any clone reuses a pass from any other, and
adding a name to `local_ci_env` keeps every pass. `local-ci <pr>` reads the PR's head through REST
where GraphQL answers 403, so it runs in a cloud session too. `local-ci <pr>` and `local-ci --record` read it the
same way; the posted comment then says it was reused by tree from the pass recorded on that commit, and when, instead
of running the check again. The host stays
in the record: nothing `approvals.py` posts names it, and posted text writes the home directory `~` (public), since a
public board would show both. The posted comment names the gate's command and the variables it sets (`Env: A=1`, or
`Env: default`).
With loop.md's `head_proof` under `loop` (`status: <context>`, `env: [<names>]`), each head proves itself under env values
the lead picks (the PR's acceptance test classes), which the gate's fixed env doesn't. `local-ci --record --head-proof` runs the gate as `--record`
does, but takes each `env` name the caller's environment sets from it instead of clearing it, over the gate line's own
assignment of that name, so the command it records names the value (`GATE_UI_TESTS=LoginAcceptanceTest mise run check:all`). It
publishes the pass as the commit status `<context>` instead of `gate`, described `Tree <tree> on <commit's first 12>:
<name>=<value> ...` for the `env` names it ran with (`default` for none), so the verifier reads the values off the head.
`land` reads it, the one status a decision reads for a pass: a green status of that context on the head commit itself, naming the head's tree,
by the account running this or a writer. A status on an older head never counts, since a status belongs to one commit.
Which tests prove a change is the project's rule (the PR's own acceptance tests); `land` checks only that a head proof exists.
Narrowed mode: loop.md's line `Affected tests: <command>` without the line `Gate: full`, the opt-out (narrowing). Its
command is the whole narrowed check: the project's selector plus its cheap whole-repo lint and type checks, reading the
base from the variable `Gate base:` names, else `GATE_BASE`. `affected-tests` runs it against `--base <rev>` (a PR's
base SHA, a push's previous tip), else the merge base with the branch's base. Where it can't tell exactly, it runs
the narrowest set it can still name (#2217): a `<rev>` git can't resolve (all zeros, a new branch) selects against the
merge base, and a loop.md without the line runs the `select` cut, then `mise run check` for a change the cut can't
settle; zero runs only for a change of non-product paths, saying `0 selected: non-product`. The opt-out runs `mise run
check`. Under
`ci: false`, `local-ci --record` runs it against `--base`, else the merge base, and `local-ci <pr>` posts that pass on
the head's tree, against the merge base of the head and the PR's base; `check merge` reads no local-ci pass (#1872). A
narrowed pass's command is the variable's assignment, the command, then ` (narrowed)`, which no gate command can hold,
so its record and key never equal a full pass's, and nothing that wants a full pass takes it. `affected-tests
--record` records its pass the same way (local_ci under narrowed_to its base), and `gate-record <head> -- <the narrowed
command>` reads it, its last word `(narrowed)`. Its `gate` status and record branch say `narrowed vs <base>`, and `local-ci --status` says `narrowed against <base>`. A project with
`gate_steps` keeps its steps everywhere, since each narrows by its `paths`. Without the `Affected tests:` line, `check
build` says in one line that every gate runs the full check (not with the opt-out, which chose it).
Deprecated: with loop.md's `carry_over_main` section (`except: <globs>`), merges of the base into a PR head H keep H's proofs:
each commit from the head H' down its first-parent line to H is a merge whose second parent is on the base (an
ancestor of its tip), whose tree is what `git merge-tree --write-tree` makes of its parents (no conflict resolved,
nothing added), and where the base's changes since the merge base touch no file the PR's side changes and no path an
`except` glob matches (fnmatch, as a rule's `paths`: the project's merge-check configuration, such as lint, type,
test and gate config and lockfiles). So a PR commit after H, new, edited or rebased, carries nothing. Then H's newest
verdict covers H' (`check merge` and `land`, the verifier's `Risk:` line included). It never carries
a `local-ci` record: H' runs its own gate, as CI would run on it. `check merge` and `land` print one line per merge
naming it, its first parent and the predicate, or why the head carries nothing. A git error, a second merge base or
any failed condition carries nothing: the head needs its own verdict, as without the section, where `check merge` and `land` read no git for it.
Every comment and review `approvals.py` posts is checked first against a local deny list, the file the environment
variable `LOOP_DENY_LIST` names (denied): a line naming a listed term refuses the post before any call to GitHub.
`verdict` posts a report in the verifier role's format as one PR review (event COMMENT, since GitHub refuses the others on
one's own PR) on the commit its `Head:` line names, carrying any text after the pass number on its `Verifier:` line
(a qualifier, such as why the verifier shares the author's family) into the review's first line, refusing a report without that line or its
`Verifier: <family>, pass <n>` line or its `Risk:` line, a `### ` heading with a `(<severity>)` it can't read as a finding, or finding
headings that don't add up to the `VERDICT:` counts (any other `### ` heading is a sub-heading and posts nothing), a report
with no `## Criteria` section or an empty one, an `Accepted on evidence: <line> — <proof log>` line naming no proof log,
and any `Accepted on evidence:` line in a report whose `Risk:` is not `low`, naming it, since a manual line on a high-risk
change is probed by hand: one
inline comment per finding, moved to the nearest line of the diff when its
line is outside it and saying so, and in the review's body when its file is outside the diff. The review's body
carries the `## Criteria` section first and each `Accepted on evidence:` line before the closing lines.

An exact revert skips the spec and the verdict: a PR whose body has a line `Reverts #<n>` (GitHub's Revert button writes
`Reverts <owner>/<repo>#<n>`), where #n is a merged PR and, file for file as GitHub's diff of each shows them, the PR
removes what #n added and adds what #n removed (files, and each run of lines in order; line numbers and context may
differ), and every path either touches has at the PR's merge base the mode it had after #n and at its head the mode
it had before #n, absence included. A file GitHub shows no diff of (binary, too large, only renamed or moded) is never
exact. Its code returns to a state already specced and reviewed, so `check build` passes it and `check merge` asks
only for Evidence (what went wrong), no unresolved thread or review requesting changes, the base's required checks
green and the label where the change is high risk, which for a revert also means `risk:high` on an issue it names or on #n, or #n's
last verdict judging it high, since undoing a high-risk change changes as much as making it.
A `Reverts #<n>` PR that is not exact gets a line saying why, then every proof of any PR.

A spec or plan approval is a person's: a comment `approve --by owner` writes on the owner's word, which the gate
reads, plus the `approved:<point>` label for the board. No agent writes one, since an agent approving its own work
is no approval. Where the policy on origin's default branch sets `loop.delegate: all`, the owner's standing
delegation covers specs and plans too: `approve <spec|plan> <issue> --by delegate` writes the record with `By:
delegate` under the line `Approved under the owner's delegation.`, adds the label and removes the issue's
`needs-owner`, the spec's hold; without the setting it is refused. `loop.delegate_spec: false` (default true) keeps
specs and plans the owner's under `delegate: all`: `approve <spec|plan> --by delegate` is refused, naming the key, and
a delegate's record counts for nothing, while the delegate's go on a merge is unchanged. The gate counts a delegate's
current record in place of the owner's only while the policy it reads sets `delegate: all`, no `delegate_spec:
false`, and the PR touches no path `risk.critical_paths` names, which stays the owner's, and otherwise names why it
doesn't count. `approve breakglass` stays the owner's
alone. The owner's record:

    Approved: spec
    By: owner
    Spec: sha256 9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08      (plan: Plan:)

A spec or plan is named by its text, the sha256 of the issue's or comment's body as GitHub stores it (line ends read
as `\n`), so any change to the text voids the approval, read through GraphQL or REST alike, and restoring
exactly the approved text restores it, since the text is then the one approved. A record written before
texts were named carries the last edit as GitHub's edit history shows it (`as edited <time>`, `as written <time>`
before any): GraphQL still dates it, so it holds on a host until the next edit; REST has no edit time, so there it is
refused with the command that renews it. Every approval is a new comment: a re-approval says the spec or plan
changed, and `approve` minimizes as outdated each earlier record, which it supersedes, where GraphQL says this
account may. With `owner: <login>` under loop.md's `loop`, `check build`, `check merge` and `land` record an
`approved:spec` (or `approved:plan`) label that login added after the spec's (plan's) last edit as `approve --by
owner` would, removing `needs-owner`, and say so: the label is the owner's word. One they added before the last edit
is named and changes nothing, nor does anyone else's. Over REST the last edit is the spec's `updated_at`, which every
edit moves (and any other change), so a label is recorded only when nothing changed the spec after it.

GitHub is read through GraphQL, the fast path, and through REST where GraphQL answers 403, as a cloud session's proxy
does, or marks fields FORBIDDEN, as a job token in a private organization repository gets: after one refused call a
run reads REST alone, the same fields call by call. REST has no closing links, so a PR's issues are its body's
`Closes`, `Part of` and `Implements` lines; its review threads come from the proxy's `pulls/<n>/ccr/review_threads`
route, or, where GitHub itself answers 404 there, from GraphQL asked for the threads alone, and where neither answers
the read fails rather than count none; a commit
co-author, which only GraphQL resolves to an account, exempts no PR from review; and whether a PR is in a merge queue
can't be read, so `land` refuses it there. The proxy refuses the collaborator lookup too, so there only the account
running this counts as a writer, by its own permissions on the repo.

The policy block (policy.md beside SKILL.md has every key), YAML, holds the owner's standing policy, checked against SCHEMA: a
section or key it doesn't know or a value of the wrong type is refused, naming the key, and so is a file without
the block. `loop` holds `cap`, `ci`, `opt_out`, `gate`, `local_ci_env`, `owner`, `machine_exit`,
`short_seconds` (mutate.py's), `pre_land` and `head_proof`; `mutate` holds `inconclusive_exit` (mutate.py's); `spec` holds `auto`; `levels` holds `low`,
`high` and `critical`, each with `spec` and `merge` (above), and `rerun`, whether `check merge` and `land` need an
independent rerun of the head's selection on its tree (default false for `low`, true for `high` and `critical`): a
record of `local-ci --record --rerun` on origin that an app pushed (a CI run on the head) or that a verifier pass on
the head names on a `Rerun: <record commit>` line (a fresh session the verifier started), never the worker's own
record (rerun_gap).
`spec.auto` as a list approves by policy, with no record or label, the spec of an issue of each kind it names:
`bug`, labelled a bug; `follow-up`, carrying a `Found while #<n>` line in its body (a follow-up an agent raised);
`sub-issue`, a native sub-issue of an epic with the owner's current spec record and `approved:spec`, opened by an
account GitHub's collaborator permission gives admin, maintain or write. A high-risk one is still built; its merge
waits for what its level asks. `spec.auto: true` approves every spec the same
way, epics included, except one opened by an account without write access, or one rated before the build at a level
whose `spec` is `owner` (by default `high` there: the risk rule on the labels of the issue and the PR and the PR's
files, at `check build` the files pushed so far): those need the owner's record and label, and `check` says why. A
level asking `owner` of the spec holds a spec a `spec.auto` kind covers too. Under `spec.auto: true` the risk rule
holds the spec instead of the merge by default, since the owner approved the change before it was built; `risk:high`
and the `verifier` term still hold the merge. The list may also
hold a `paths` term (`- paths: [mise.toml, docs/agents/**]`), globs as a rule's `paths` are: it approves by policy the
spec of an issue labelled `spec:paths`, which the lead adds to declare that the change stays inside them, since the
gate can't tell that from the spec alone and the setting alone would approve every spec until its merge. `check build`
and `check merge` judge it on the issue's and the PR's labels and every path the PR touches, at `check build` before
the PR the branch's files so far: it approves only while every path matches a glob, a moved file's old name as well
as its new one, so a rename across the boundary either way voids it; GitHub gives the whole file list and every old
name; neither carries `risk:high` nor the risk rule holds (its `verifier` term too, once a review is read); and the
issue's author can write to the repo. Otherwise the owner approves the spec, and `check` names why, the files outside
the globs included. The label without the term approves nothing. Without `auto`, or
with `false` or an empty list, the owner approves every spec, and the gates are the merge approval of a change whose level
asks `gates` of the merge.
Whatever `spec.auto` says, a migration row's issue that nothing above covers is covered by its parent's approval: a
native sub-issue whose body opens with `Part of #<parent> (row <name>)`, naming that parent, whose spec carries
`approved:spec` and a current record as a direct link's would (the owner's, or the delegate's where it counts), opened
by an account that can write to the repo, where neither the issue nor the PR carries `risk:high` and neither the risk
rule nor `risk.critical_paths` holds on the PR's files. `check` names the parent and the row it credits, or the piece
missing from a row it doesn't; the verifier checks the row against the parent's table.

A rule (`risk`, `plan`, `signoff`) is a section of terms, any one of which matches: `always: true`; `labels`,
label names matched exactly on the PR and its issues; `paths`, globs matched on the PR's files; and in `risk` alone
`verifier: true`. `risk.critical_paths` (deprecated name `merge_paths`, read with a `deprecated:` line; both set is
refused), globs matched on the PR's files as `paths` are, is no term of the rule: a PR touching one rates `critical`
whatever its labels, `spec.auto` and the verifier's `Risk:` line say, and by default only the owner's go
on its head lets it merge, into any branch, so `check merge` and `land` wait for it, `approve merge
--by delegate` and `delegate` refuse it, and the label is no go; files it can't read hold. A list holds strings. The gate reads the keys, never the words of a rule, so a path or
label can never read as a term. A glob matches every path the PR touches on either side of the change: a
deleted file's, and a renamed or copied file's old name as well as its new one. Paths it can't judge, past the one
page of files it reads or an old name GitHub won't give, hold. The `plan` rule asks
for the owner's plan approval, and has no `paths`, since a plan comes before the change. A `bot.<login>` section,
such as `bot.dependabot[bot]` with `paths` uv.lock and **/package.json, lets that bot's PR merge without a verifier
review when every commit is the bot's (author, and a committer that is the bot or no account) and each file it changes, none renamed or copied,
matches one of its paths (exempt); with `branch: renovate/` it names an updater opening PRs under a person's token by
its head branch instead, and a `verify` label refuses either; without one, every PR needs the review. The `signoff` rule says whose change the
owner signs off in use after it lands: `check merge` needs the PR to link each issue it matches, or whose spec has a
line `Sign-off: owner`, with `Implements #<n>`, which leaves it open for the sign-off, and every other issue with a
closing keyword (`Part of` suits both); without a `signoff` section no keyword is checked and `Implements` is a link like
`Part of`. `verifier` holds at the merge (under `spec.auto: true` too, since the verdict comes after the build) when the
verifier judges the change high: the highest `Risk:` of the verifier's passes since its last `Scope: full` pass (its first pass counts as one), by
a trusted account (passes), is `high`, any word but `low`, or missing, and the hold quotes it. A head no review covers waits for its review instead; a
PR that needs none (exempt, an exact revert) has no judgment. A `risk:high` the gate added for that term comes with a
comment saying so, and the gate takes the label off once a later `Scope: full` pass says `Risk: low`, unless it was added again
after that comment, which is a raise only the owner lowers. Paths in the same rule hold whatever the verifier says.
The board's `Risk` field follows the label and the verifier's word, and a field at `High` adds the label (flag); the
label stays the gate's only input, and a board it can't reach is a line, never a failure.
The spec is the issue body, or its last comment whose first line is `## Spec`, where a tool owns the body. A soft gate against a forgotten step, not a security boundary: the agent holds the same
credentials as the person. issue-tracker.md's components and extra categories are the keys of its settings block's
`components` and `categories` (tracker_settings). Reads and writes through `gh`. Stdlib, and PyYAML for the settings
block (SKILL.md § Settings says how it is delivered).
"""

from __future__ import annotations

import argparse
import ast
import atexit
import contextlib
import contextvars
import fcntl
import fnmatch
import functools
import hashlib
import importlib.util
import io
import itertools
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path
from types import ModuleType
from typing import Any, NamedTuple, NoReturn

try:
    import yaml
except ImportError:  # run as `python3 approvals.py` where PyYAML isn't installed
    sys.exit(
        f"{Path(sys.argv[0]).name} needs PyYAML: run it with `uv run {sys.argv[0]}`, which installs it"
    )

# the ready state: an approved spec (board: Ready); waiting for the owner
READY, NEEDS_OWNER = "approved:spec", "needs-owner"
# a hold put on a PR by its lead, or a claim by another coordinator: land skips it, and Mergify takes none carrying it
HOLD = "loop:hold"
BUG, EPIC = "kind:bug", "epic"
# the lead's claim that a spec's change stays inside `spec.auto`'s `paths`, which the policy then approves (scoped)
SCOPED = "spec:paths"
CATEGORIES = {BUG, "kind:enhancement", "kind:chore", "kind:documentation"}
# a component's label is `area:<name>` and a category's `kind:<name>`; the docs name them bare or in full (qualified)
PREFIXES = {"component": "area:", "category": "kind:"}
SIZE_ORDER = ["size:XS", "size:S", "size:M", "size:L", "size:XL"]
SIZES = set(SIZE_ORDER)
TRACKER, LOOP = Path("docs/agents/issue-tracker.md"), Path("docs/agents/loop.md")
# the gatekeeper's policy, under `## Policy`; loop.md's `## Settings` block is read without it, deprecated
POLICY = Path("docs/agents/gatekeeper.md")
POINTS = ("spec", "plan")
# the `By:` of the delegated approver's merge record (delegate); on the default branch it counts only with
# `loop.delegate: all` (owned)
DELEGATE = "delegate"
# the plain line atop the merge records `approve merge` writes, by `--by`, so a person reads off the PR whose go it is
VOICED = {"owner": "Approved by the owner.", DELEGATE: "Approved under the owner's delegation."}
# the description of the delegate's OWNER_GO status, after `The delegate's go on <sha>`
DELEGATED = ", under the owner's delegation"
MERGE_LABEL = "approved:merge"
# the go on a high-risk head, the owner's or the delegate's, a commit status on that one commit (approve merge), so no
# push carries it over but merges of the base a verifier pass says change no logic (carried_go)
OWNER_GO = "owner"
# the verdict line a verifier pass on a merge-only delta writes, `Logic change: none`, which carries the go (carried_go)
LOGIC = "Logic change"
# an independent rerun of the head's selection (levels' `rerun`, rerun_gap): the variable `local-ci --record --rerun`
# sets in front of the command it runs, so its record names it in its command and `Env:` line and is never the worker's
# record of the same command (its key differs); and the verdict line `Rerun: <record commit>` by which a verifier names
# the record a fresh session it started pushed
RERUN_VAR = "LOOP_RERUN"
RERUN_LINE = "Rerun"
# the account whose push of a rerun record counts as the CI run's (rerun_gap), unless `loop.rerun_pusher` names another:
# GitHub Actions' workflow token
RERUN_PUSHER = "github-actions[bot]"
# how an OWNER_GO status the gate once turned from the `approved:merge` label ends: no go, since the label can be a
# delegate's
LABELLED = f"from their `{MERGE_LABEL}` label"
# a change's consequences, not its urgency (that is the tracker's priority): set by a person or at triage, or by the gate
RISK_LABEL = "risk:high"
# a follow-up an agent raised: the agent puts this line on every issue it files (policy.md, Rules, `spec.auto`)
FOUND_WHILE = re.compile(r"^[ \t>*-]*Found while[ \t]+#\d+", re.MULTILINE | re.IGNORECASE)
# a migration row's claim, the line its sub-issue's body opens with: `Part of #<parent> (row <name>)`, a row of the
# parent's approved table, which then covers its spec (row_gap)
ROW = re.compile(r"\A\s*Part of #(\d+) \(row ([^)\n]+)\)", re.IGNORECASE)
# check merge's exit while it waits for `check` or the owner; argparse's usage error is 2
WAITING = 3
# a verifier pass's PR review starts with this; same-family included, as `Verifier (claude, same-family), pass 2`
PASS = re.compile(r"Verifier \((.+?)\), pass (\d+)")
# a tier below the default a `Verifier model:` line names, a word of its own after the signature in the review's
# family (`codex/terra-6.1/med mid`, SKILL.md § Verifier report): a model id holding the word (`sol-mid-preview`) names no tier
LOWER_TIER = re.compile(r"(?<![^\s,])(mid|small)(?![^\s,])", re.IGNORECASE)
# The cap, the last pass the loop runs on its own (a core finding open after it is the lead's call), is defined once:
# its default in SKILL.md § The cap, a project's own in loop.md's `cap` setting.
CAP_DEFAULT = re.compile(r"^The cap is (\d+) passes\b", re.MULTILINE)
# the lead's decision at the cap to land with majors open, filed as a follow-up issue, as a PR body line of its own:
# `Cap: #<follow-up>` (SKILL.md § The cap); a quoted line (`>`, a code span or block) is an example, not the record. ponytail: the gate trusts the number the lead names; fetch the issue and
# check its `Found while` line if a lead ever names one it never filed.
# at most 3 spaces before it, since 4 or a tab make the line an indented code block: an example, not the decision
CAP_DECISION = re.compile(
    r"^ {0,3}(?:[*-][ \t]+)?\**Cap\**:?\**[ \t]*#\d+\b", re.MULTILINE | re.IGNORECASE
)
# the first line of the record claim() leaves when it assigns an issue, which release() reads
ASSIGNED = "Assigned: @"
# GitHub's closing keywords, plus two links that leave the issue open: `Part of` for a spec the PR builds on but
# leaves unfinished, and `Implements` for one the owner signs off after landing (`signoff`). ponytail: same-repo `#n`
# only, add owner/repo#n and issue URLs when a PR here uses one.
CLOSING = re.compile(
    r"\b(?:(close[sd]?|fix(?:e[sd])?|resolve[sd]?)|(part[ \t]+of)|(implements)):?[ \t]+#(\d+)\b",
    re.IGNORECASE,
)
# a link's kind, in the order one wins when a body names an issue twice: GitHub closes on any closing keyword
CLOSES, IMPLEMENTS, PART = "closes", "implements", "part of"
# the spec's sign-off record, as a line of its own: `Sign-off: owner` or `Sign-off: none`
SIGNOFF = re.compile(
    r"^[ \t>*-]*\**Sign-off\**:?\**[ \t]*`?(owner|none)\b", re.MULTILINE | re.IGNORECASE
)
# code (a ``` or ~~~ block, a code span) and comments: an example, not a closing line. A block closes on a line of
# its fence character at least as long as its opening, CRLF included; an unclosed one quotes nothing, since what
# Markdown closes it on (the end of a quote or list) is beyond a regex. A ``` opening with a later backtick is a span.
QUOTED = re.compile(
    r"^[ \t>]*(`{3,})(?![^\n]*`).*?^[ \t>]*\1`*[ \t]*\r?$"
    r"|^[ \t>]*(~{3,}).*?^[ \t>]*\2~*[ \t]*\r?$"
    r"|`[^`\n]*`|<!--.*?-->",
    re.DOTALL | re.MULTILINE,
)
# the first line of the comment flag() leaves when the verifier's `Risk:` line made it add `risk:high`, which says the
# label is the gate's to take off again when a later pass judges the risk low
FLAGGED = f"`{RISK_LABEL}` added by the gate"
# the board's single-select field holding a PR's risk beside its `risk:high` label (the board template's)
BOARD_RISK = "Risk"
# a revert's line, as GitHub's Revert button writes it or by hand; ponytail: one revert per PR, the first line counts.
REVERTS = re.compile(r"^[ \t]*Reverts[ \t]+([\w.-]+/[\w.-]+)?#(\d+)\b", re.MULTILINE)
CHECKED = "Local check passed"
# a gate pass published as a commit status: its context, and its description (described): tree, commit, command
PUBLISHED = "gate"
# where publish() pushes a pass in origin, the one route another clone reads it by: a branch, since a cloud session's
# proxy refuses a push outside refs/heads/; land deletes it once no head needs it (drop_records)
RECORD_BRANCH = "refs/heads/loop-gate/{}"
# how old a record branch whose tree is no open PR's head must be before land deletes it: a record may be pushed
# before its PR is opened
RECORD_AGE = 24 * 60 * 60
# a pushed record's author and committer: no person's name or address reaches the repo through it
RECORDER = {"GIT_AUTHOR_NAME": "loop", "GIT_AUTHOR_EMAIL": "loop@invalid"} | {
    "GIT_COMMITTER_NAME": "loop",
    "GIT_COMMITTER_EMAIL": "loop@invalid",
}
PASSED = re.compile(r"Tree ([0-9a-f]{40,64}) on ([0-9a-f]{7,64}): (.+)")
LOG_TAIL = 30
CHANGED = {
    "spec": "The spec changed after the last approval, so it was checked again.\n",
    "plan": "The plan changed after the last approval, so it was checked again.\n",
}
GREEN = {"SUCCESS", "NEUTRAL", "SKIPPED"}
# a finished check run or a set status that is not green: it turns green only when run again, never by waiting
RED = {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE", "STALE"}
# the commit status the project's approvals workflow posts: check merge's own output, under its name before #614 and,
# for one release line, also under the gatekeeper's; the gate accepts either and waits for neither (OWN, below)
APPROVALS, GATEKEEPER = "loop:approvals", "gatekeeper"
# the commit status land posts on a head right before its merge call; a base that requires it must not make land wait for it
LAND = "land"
# every status the loop posts itself: a wait on the base's required checks never waits for them, a head_proof can't take them
OWN = {APPROVALS, GATEKEEPER, LAND}
# a URL in a `## Demo` comment; gh's --attach embeds a video or image as a bare user-attachments one, which plays inline
URL = re.compile(r"https?://[^\s<>()\[\]\"'`]+")
ATTACHMENT = "https://github.com/user-attachments/"
# land --wait: seconds between tries; a landing lock older than CEILING is stale, whatever the policy's `timeouts` make
# land --wait's own ceiling (ceiling_of)
POLL, CEILING = 30, 2 * 60 * 60
# the policy's `timeouts` where it sets none: today's values, one test and one selection run (#2228)
TIMEOUTS = {"test": "60s", "run": "60m"}
# the exit of a run stopped at its budget (run_heavy), coreutils `timeout`'s: a timeout, never a test's failure
TIMED_OUT = 124
# run_heavy's output for a run it stopped at its budget, so a command's own exit 124 is never read as one
SPENT = object()
# land --wait runs landing() (a GraphQL read of every named PR's PULL, 5 points each, and more) again only this many
# seconds after the last run, unless it waits on something its probe watches (FULL): a merge queue or a check takes minutes to move, so
# 30 s polls spend the account's quota, and its per-minute limit, on answers that did not change. POLL is the step
# the sleep goes in, so the log is never silent and a lock that moved is seen at once
QUEUE_POLL = 300
# between two landing() rounds a wait probes, each POLL, only what it waits on (#1915): a held lock with one `git
# ls-remote` of its ref, which costs no API quota, running landing() again only when the lock moves past a heartbeat
# (restamped), or has not moved for STALE seconds (its land died, so the round breaks it), never on FULL; a wait on CI
# with one
# ls-remote of the base and the named PRs' heads, and an ETag read (rest_if_changed: a 304 costs no quota) of each
# head's check runs and statuses, running landing() again once a head, the base or a required check's state moved.
# Nothing else a round reads can end such a wait, so the round runs again only after FULL seconds otherwise
FULL = 900
# what the last landing() round waited on, for that probe: the base, the named PRs and the base's required checks
# and whether the round left the free lock to another land (locked: an older waiter, or lands that kept taking it),
# so the probe also watches the lock only then: a CI wait on the local queue frees the lock each round, and lands that
# all watched it would wake each other
watched: dict[str, Any] = {
    "base": "",
    "pulls": [],
    "required": set(),
    "deferred": False,
    "queued": {},
}
# a wait on named PRs a merge queue holds (GitHub's or Mergify's: `watched["queued"]`, PR -> which) reads only what ends
# it (#2101): each QUEUE_WATCH seconds one queue_watch call, the PRs' state, head, queue entry and labels, and runs the
# round once one merged, closed, left the queue or was pushed to. A queue merges in minutes, and a round re-reads the
# rules, the permissions and the merged high-risk PRs, none of which moves while a PR waits in it
QUEUE_WATCH = 60
# the poll while the account's remaining quota is under LOW_QUOTA of its limit: waiting lands then ask GitHub less often
LOW_POLL, LOW_QUOTA = 600, 0.1
# the longest poll of a wait whose state stays the same round after round: QUEUE_POLL doubles up to it (#1771). With
# POLL_JITTER it stays inside the twice-300 s window a `land` event is live in (events.md) and under WAITER_STALE, so a
# quiet wait never reads as a dead one; a lock that moves is still seen at once, since the sleep checks it each POLL
QUIET_POLL = 480
POLL_JITTER = 60  # most seconds of random delay added to a land --wait poll
# a wait that meets this many network failures in a row (NETWORK) fails with the last one
MISSES = 3
# what git and gh print when the network, not the request, failed: a name that did not resolve, a dropped or timed-out
# connection, a ref listing cut short; an HTTP refusal (`unable to access … error: 403`, `RPC failed; HTTP 404`) is no
# network failure, so a revoked token or a renamed repo fails at once
NETWORK = re.compile(
    r"Could not resolve host|RPC failed; curl \d+|Recv failure|expected flush|Operation timed out"
    r"|Connection (?:timed out|reset|refused)|Failed to connect|early EOF|Network is unreachable"
    # gh's own client words a failed connection differently from git's curl
    r"|error connecting to|dial tcp|no such host|i/o timeout|connection reset by peer",
    re.IGNORECASE,
)
# (lock ref, tip) of the last HELD lock locked() waited on; land() polls it without GitHub calls
held_tip: tuple[str, str] | None = None
# the signals that stop a land and release its lock (stopped)
STOPS = (signal.SIGTERM, signal.SIGHUP)
# the landing lock of a base: a branch that only moves forward, since a host may be allowed only to create and
# fast-forward a branch (a cloud session's proxy refuses a push outside refs/heads/ and every delete); its tip's title
# says whether it is held, and [skip ci] in it starts no workflow
LOCK = "refs/heads/loop-landing/{}"
HELD, FREE = "landing lock: held", "landing lock: free"
# a land re-stamps its lock every BEAT seconds; a lock that says it beats (BEATS in its body) and has not been stamped for STALE
# seconds was left by a land that died, and is broken. A lock without the word is a land of an earlier version's: CEILING.
# waiting order (#1216): a `land --wait` keeps one ref per waiting land, stamped at every poll, holding when it began to
# wait; a free lock goes to the longest waiter that still stamps (younger than WAITER_STALE, above a poll with its jitter)
WAIT_REF = "refs/heads/loop-waiting/{}/{}"
WAITER_STALE = 600  # above the longest gap between stamps: LOW_POLL + POLL_JITTER, and the reads before the lock
waiting = False
waited = {"since": 0.0, "id": "", "base": "", "tip": "", "took": False, "idle": False}
WAIT_ENV = "LOOP_LAND_WAIT"  # the place in the waiting order, across a land run again on the base's new code
BEAT, STALE, BEATS, BEAT_PUSH = 300, 900, "beats", 120
beating = (
    threading.Lock()
)  # one push of the lock at a time: the heartbeat's, the repointing's, the release's
# the lock before #814, a ref outside refs/heads/: a land waits while it is on origin, never writing it, so a host still
# on that version and this one never land at once
LEGACY = "refs/loop/landing"
# how ci() ends its refusal of a head with more checks than the one page it reads, which no CI run clears
PAGED = "CI checks: the gate reads one page"


def links(body: str) -> dict[int, str]:
    """The issues a PR body names after a closing keyword, `Part of` or `Implements` on the same line, in order, each
    once, with the link's kind (CLOSES, PART, IMPLEMENTS); a closing keyword wins over the others, since GitHub closes
    the issue on it whatever else the body says, and `Implements` over `Part of`."""
    found: dict[int, str] = {}
    rank = (CLOSES, IMPLEMENTS, PART)
    for closes, part, implements, number in CLOSING.findall(QUOTED.sub("", body)):
        kind = CLOSES if closes else IMPLEMENTS if implements else PART
        previous = found.setdefault(int(number), kind)
        found[int(number)] = min(previous, kind, key=rank.index)
    return found


def named(body: str) -> list[int]:
    """The issue numbers a PR body links (links), in order, each once."""
    return list(links(body))


def section(text: str, heading: str) -> str:
    """The text under `## <heading>`, up to the next `## ` heading; empty when there is none."""
    found = re.search(
        rf"^## {re.escape(heading)}[ \t]*\n(.*?)(?=^## |\Z)", text, re.DOTALL | re.MULTILINE
    )
    return found.group(1) if found else ""


def qualified(kind: str, name: str) -> str:
    """A component's or category's label: `billing` and `area:billing` both name `area:billing`."""
    prefix = PREFIXES[kind.lower()]
    return prefix + name.removeprefix(prefix)


def label_names(tracker: str) -> dict[str, set[str]]:
    """The loop's label rule: each kind's names, the loop's fixed set plus docs/agents/issue-tracker.md's, a
    component as `area:<name>` and a category as `kind:<name>`."""
    found = tracker_settings(tracker)
    return {
        "category": CATEGORIES
        | {qualified("category", name) for name in found.get("categories", {})},
        "component": {qualified("component", name) for name in found.get("components", {})},
        "size": SIZES,
    }


class Table(dict):
    """A settings section whose every key is required but those `optional` names, such as `land`."""

    def __init__(
        self,
        *args: Any,
        optional: tuple[str, ...] = (),
        together: tuple[str, ...] = (),
        **kinds: Any,
    ) -> None:
        super().__init__(*args, **kinds)
        self.optional = frozenset(optional)
        self.together = together  # optional keys that are all set or all unset


class Choice(tuple):
    """A setting that is one of these names, such as `loop.delegate`, or one of its `aliases` (an old or a longer
    name, mapped to the name it stands for)."""

    aliases: dict[str, str]  # set where a setting has some; read through getattr


class Rows(Table):
    """A setting that is a list of sections, each holding these keys, such as `loop.gate_steps`; `unique` names the
    keys no two rows may share."""

    def __init__(self, *args: Any, unique: tuple[str, ...] = (), **kinds: Any) -> None:
        super().__init__(*args, **kinds)
        self.unique = unique


# where `land` may run (loop.md's `land.where`): a host, a cloud session (CLOUD), or either
PLACES = ("host", "cloud", "both")
LAND_WHERE = Choice(PLACES)
LAND_WHERE.aliases = {
    "claude-cloud": "cloud"
}  # the name `cloud` had before it named only Claude's cloud
# where a gate step may run (loop.md's `gate_steps`): any machine, or only a host, never a cloud session (CLOUD)
STEP_PLACES = ("any", "host", "linux")
# loop.md's settings (SKILL.md § Settings), one section each: each key's type, or a tuple of the values it allows.
# A list holds strings; `bot.<login>` is a section per bot.
STAGES = ("intent", "spec", "plan", "build", "review", "land")
# the specs `spec.auto` may name: an issue that is a bug, a follow-up an agent raised, a sub-issue of an approved epic
SPEC_KINDS = ("bug", "follow-up", "sub-issue")
# the levels the risk rating gives a change, lowest first (gatekeeper.md's `levels`: what each asks of the spec and
# the merge); `low` is every change nothing raises
LEVELS = ("low", "high", "critical")
# what a level asks of the spec: `policy`, spec.auto decides; `owner`, the owner's record and `approved:spec`
SPEC_ACTIONS = ("policy", "owner")
# what a level asks of the merge, weakest first: the gates alone; a person's go on the head (the owner's, or the
# delegate's where delegation allows); the owner's own go on the head
MERGE_ACTIONS = ("gates", "go", "owner")
SCHEMA: dict[str, dict[str, Any]] = {
    "loop": {
        "cap": int,
        "ci": bool,
        "opt_out": bool,
        "local_ci_env": list,
        "gate": str,
        "gate_steps": Rows(
            name=str,
            command=str,
            where=Choice(STEP_PLACES),
            paths=list,
            on_demand=bool,
            unique=("name", "command"),
            optional=("paths", "on_demand"),
        ),
        "owner": str,
        "machine_exit": int,
        "short_seconds": int,
        "pre_land": str,
        "head_proof": Table(status=str, env=list),
        "delegate": Choice(("all", "feature")),
        "delegate_spec": bool,
        "pack_staleness": Choice(("warn", "refuse")),
        "stalled_after_hours": int,
        "rerun_pusher": str,
    },
    "mutate": {"inconclusive_exit": int},
    "spec": {"auto": SPEC_KINDS},
    "practice": dict.fromkeys(STAGES, str),
    "risk": {
        "always": bool,
        "labels": list,
        "paths": list,
        "verifier": bool,
        "critical_paths": list,
        "merge_paths": list,
    },
    "levels": {
        level: {"spec": Choice(SPEC_ACTIONS), "merge": Choice(MERGE_ACTIONS), "rerun": bool}
        for level in LEVELS
    },
    "incubating": {"append_paths": list, "waive": list},
    "plan": {"always": bool, "labels": list},
    "signoff": {"always": bool, "labels": list, "paths": list},
    "bot": {"paths": list, "branch": str},
    "land": Table(
        signed_squash_to=str,
        allowed_signers=str,
        where=LAND_WHERE,
        signing_key_env=str,
        merge_queue=bool,
        merge_commit_heads=list,
        batch_wait_seconds=int,
        batch_size=int,
        wait=str,
        queue=Choice(("mergify", "github")),
        together=("signed_squash_to", "allowed_signers"),
        optional=(
            "signed_squash_to",
            "allowed_signers",
            "where",
            "signing_key_env",
            "merge_queue",
            "merge_commit_heads",
            "batch_wait_seconds",
            "batch_size",
            "wait",
            "queue",
        ),
    ),
    "carry_over_main": Table({"except": list}),
    # the narrowed gate's first cut (classify): the project's build inputs, and the checks a non-product path still gets
    "select": Table(
        build_inputs=list,
        checks=Rows(paths=list, command=str, unique=("command",)),
        # the checks whose pass is never reused (#2183): each a gate step's name or command, or `gate`'s command
        unkeyed=list,
        # how many shards a whole-suite selection splits into (shard_selections), 1 or absent is one
        shards=int,
        # what the merge queue's selection does with a build input (#2445): `floor`, the default, the other changed
        # files' tests plus a lockfile's importers; `full`, the whole suite
        build_inputs_in_queue=Choice(("floor", "full")),
        optional=("build_inputs", "checks", "unkeyed", "shards", "build_inputs_in_queue"),
    ),
    # every time limit of a run (timeouts): one test, and one selection run, the queue's check included (#2228)
    "timeouts": Table(test=str, run=str, optional=("test", "run")),
    # the break-glass land's proof windows (breakglass_settings); every key is optional
    "breakglass": Table(
        max_age=str,
        max_commits=int,
        smoke=str,
        earlier_runs=int,
        artifact=str,
        optional=("max_age", "max_commits", "smoke", "earlier_runs", "artifact"),
    ),
    # each port's adapter by name (ADAPTERS), `github` for a port not named
    "adapters": Table(
        tracker=str, forge=str, evidence=str, optional=("tracker", "forge", "evidence")
    ),
}
# docs/agents/issue-tracker.md's settings: in each section every key is a name, its value a description or a new name
TRACKED: dict[str, Any] = dict.fromkeys(
    ("components", "categories", "labels", "renames", "board"), str
)
# the settings block: a docs/agents/ file's first fenced `yaml` block, under its `## Settings` (gatekeeper.md: `## Policy`)
# heading
SETTINGS = re.compile(r"^```yaml[ \t]*\n(.*?)^```[ \t]*$", re.DOTALL | re.MULTILINE)
KINDS = {bool: "true or false", int: "a whole number", list: "a list of strings", str: "a string"}


class Policy(str):
    """docs/agents/gatekeeper.md's text, whose block settings() reads under `## Policy`; a plain str is loop.md's,
    read under `## Settings`."""


def source(policy: str | None) -> str:
    """The file the policy was read from, as its messages name it."""
    return "gatekeeper.md" if isinstance(policy, Policy) else "loop.md"


def block(text: str, file: str, heading: str = "Settings") -> dict[str, Any]:
    """The file's settings block, parsed as YAML; refused without one under `## <heading>`, since a file still on the
    line form would otherwise lose its settings unnoticed, or when it is no YAML mapping. Only that section is
    searched, so a YAML example elsewhere in the prose is never read as the settings. A key set twice is refused,
    since YAML would keep the last one silently."""
    found = SETTINGS.search(section(text, heading))
    if found is None:
        raise Refused(
            f"{file} has no ```yaml settings block under ## {heading}: the bullet form is no longer read. Write the "
            f"policy as a fenced yaml block under ## {heading} in docs/agents/{file} (SKILL.md § Settings has its "
            "keys) and land that migration on the base before the pin bump, since the policy is read from origin/<base>"
        )
    try:
        once(yaml.compose(found[1], Loader=yaml.SafeLoader), file)
        data = yaml.safe_load(found[1])
    except yaml.YAMLError as exc:
        raise Refused(f"{file}'s settings block is not YAML: {exc}") from None
    if data is None:  # an empty block: no settings
        return {}
    if not isinstance(data, dict):
        raise Refused(f"{file}'s settings block is not YAML sections of `key: value`, but {data!r}")
    return data


def once(node: Any, file: str, path: str = "") -> None:
    """Refuse a key set twice in the same section of the block's YAML node tree, naming it."""
    if isinstance(node, yaml.MappingNode):
        seen = set()
        for key, value in node.value:
            if key.value in seen:
                raise Refused(f"{file}'s `{path}{key.value}` is set twice")
            seen.add(key.value)
            once(value, file, f"{path}{key.value}.")


def sections(data: dict[str, Any], file: str) -> dict[str, dict[str, Any]]:
    """The block's sections, refusing a key outside one, since every setting belongs to a section. A section with no
    keys under it is empty."""
    for name, value in data.items():
        if value is None:
            data[name] = {}
        elif not isinstance(value, dict):
            raise Refused(f"{file} has a setting `{name}` outside a section")
    return data


def typed(section: dict[str, Any], schema: Any, name: str, file: str = "loop.md") -> dict[str, Any]:
    """A section's values checked against its schema (a map of keys, or one type for any key), refusing one that
    doesn't match it and naming the key, so a mistyped setting is never read as a rule nobody wrote. Types match
    exactly, which also refuses what YAML 1.1 reads as another type where a name was meant (`no` as false, `1.10` as
    a float), naming the key, since such a value is never the name written."""
    for key, value in section.items():
        where = f"`{name}.{key}`"
        if not isinstance(
            key, str
        ):  # `on:`, `no:` or `1.10:` as a name is read as a boolean or a number
            raise Refused(f"{file}'s {where} is no name: quote it")
        kind = schema.get(key) if isinstance(schema, dict) else schema
        if kind is None:
            raise Refused(f"{file} has an unknown setting {where}")
        if isinstance(kind, Rows):  # a list of sections, such as `loop.gate_steps`
            if not (
                isinstance(value, list) and value and all(isinstance(row, dict) for row in value)
            ):
                raise Refused(
                    f"{file}'s {where} must be a list of sections each holding `{'`, `'.join(kind)}`"
                )
            for row in value:
                typed(row, kind, f"{name}.{key}", file)
            for unique in kind.unique:
                seen = [row[unique] for row in value]
                if twice := sorted({v for v in seen if seen.count(v) > 1}):
                    raise Refused(f"{file}'s {where} names the {unique} {twice[0]!r} twice")
        elif isinstance(kind, dict):  # a section within the section, such as `loop.head_proof`
            if not isinstance(value, dict):
                raise Refused(f"{file}'s {where} must be a section holding `{'`, `'.join(kind)}`")
            typed(value, kind, f"{name}.{key}", file)
        elif isinstance(kind, Choice):
            if not (
                isinstance(value, str) and (value in kind or value in getattr(kind, "aliases", {}))
            ):
                raise Refused(f"{file}'s {where} must be one of {', '.join(kind)}, not {value!r}")
        elif isinstance(kind, tuple):  # true, false, or a list naming some of the tuple's values
            # `spec.auto`'s list may also hold a `paths` term, a list of globs (spec_paths)
            for term in value if isinstance(value, list) else []:
                if not isinstance(term, dict):
                    continue
                globs = term.get("paths")
                if list(term) != ["paths"] or not (
                    isinstance(globs, list)
                    and globs
                    and all(isinstance(g, str) and g for g in globs)
                ):
                    raise Refused(
                        f"{file}'s {where} term must be `paths`, a list of globs, not {term!r}"
                    )
            if not (
                isinstance(value, bool)
                or isinstance(value, list)
                and all(isinstance(item, dict) or item in kind for item in value)
            ):
                raise Refused(
                    f"{file}'s {where} must be true, false or a list of {', '.join(kind)}, not {value!r}"
                )
        # bool is an int to Python, so a type is matched exactly
        elif type(value) is not kind or (
            kind is list and not all(isinstance(item, str) for item in value)
        ):
            raise Refused(f"{file}'s {where} must be {KINDS[kind]}, not {value!r}")
        elif kind is str and isinstance(schema, dict) and not value:
            raise Refused(
                f"{file}'s {where} is empty"
            )  # a name's description may be empty, a path may not
    if isinstance(schema, Table) and (
        missing := [
            key
            for key in schema
            if key not in section
            and (key not in schema.optional or any(other in section for other in schema.together))
            and (key in schema.together or key not in schema.optional)
        ]
    ):
        raise Refused(f"{file}'s `{name}` lacks `{'`, `'.join(missing)}`")
    return section


def settings(loop: str | None) -> dict[str, Any]:
    """The policy's block (gatekeeper.md's `## Policy`, a Policy, else loop.md's `## Settings`), checked against
    SCHEMA: `loop`'s keys at the top level, every other section under its name and `bot` a list of `{login, paths}`;
    {} without either file, which has no rules. A file without the block is refused rather than read as one without
    rules, since a project still on the line form would otherwise lose its risk rules unnoticed."""
    if loop is None:
        return {}
    file = source(loop)
    data: dict[str, Any] = {}
    parsed = block(loop, file, "Policy" if isinstance(loop, Policy) else "Settings")
    for name, section in sections(parsed, file).items():
        if name == "bot":
            for login, bot in section.items():
                where = f"bot.{login}"
                if not isinstance(login, str):
                    raise Refused(f"{file}'s `{where}` is no name: quote it")
                if bot is None:  # a login with nothing under it
                    bot = {}
                if not isinstance(bot, dict):
                    raise Refused(f"{file}'s `{where}` must be a section holding `paths`")
                if "paths" not in bot:
                    raise Refused(f"{file}'s `{where}` lacks `paths`")
                data.setdefault("bot", []).append(
                    {"login": login, **typed(bot, SCHEMA["bot"], where, file)}
                )
        elif name == "loop":
            data.update(typed(ignored(section, name, file), SCHEMA["loop"], name, file))
            if "ci" in section:
                deprecated("ci")
        elif name in SCHEMA:
            data[name] = typed(ignored(section, name, file), SCHEMA[name], name, file)
            if data[name].get("batch_wait_seconds", 0) < 0:
                raise Refused(
                    f"{file}'s `land.batch_wait_seconds` must be a whole number, 0 or more"
                )
            if data[name].get("batch_size", 1) < 1:
                raise Refused(f"{file}'s `land.batch_size` must be a whole number, 1 or more")
            if "wait" in data[name] and name == "land":
                if span(str(data[name]["wait"])) is None:
                    raise Refused(
                        f"{file}'s `land.wait` is {data[name]['wait']!r}: write it as `2h` or `4h30m`"
                    )
                # still honoured, so a project that set it after #2199 keeps its wait (land_wait)
                deprecated(
                    "land.wait",
                    "`land --wait`'s ceiling is derived from `timeouts.run` (the queue's checks timeout plus one"
                    " more run): set `timeouts.run` instead",
                )
            for key, given in data[name].items() if name == "timeouts" else ():
                if span(given) is None:
                    raise Refused(
                        f"{file}'s `timeouts.{key}` is {given!r}: write it as days, hours, minutes and seconds"
                        " in that order, such as `90s`, `60m` or `4h15m`"
                    )
            if name == "carry_over_main":
                deprecated(name)
            if name == "risk" and "merge_paths" in section:
                # two sources of one list would drift, so the old name is read only alone
                if "critical_paths" in section:
                    raise Refused(
                        f"{file}'s `risk` sets both `critical_paths` and `merge_paths`, its deprecated name:"
                        " keep `critical_paths`"
                    )
                deprecated("risk.merge_paths", "name its globs `risk.critical_paths`")
            for port_name, adapter in data[name].items() if name == "adapters" else ():
                if adapter not in ADAPTERS:
                    raise Refused(unknown_adapter(file, port_name, adapter))
        else:
            raise Refused(f"{file} has an unknown settings section `{name}`")
    return data


DEPRECATED: set[str] = set()


# keys a policy may still set that no longer do anything, since land runs no gate (#1872): each is said once on
# stderr and dropped, so a consumer's older policy file still parses
IGNORED = {
    "loop": ("batch_queue",),
    "land": ("gate_on", "cloud_gate_timeout_seconds", "host_gate"),
}


def ignored(section: dict[str, Any], name: str, file: str) -> dict[str, Any]:
    """`section` without the keys IGNORED lists for it, each said in one line on stderr, once per run, never fatal."""
    for key in IGNORED.get(name, ()):
        if key in section and (said := f"{name}.{key}") not in DEPRECATED:
            DEPRECATED.add(said)
            print(
                f"ignored: {file}'s `{said}` does nothing, since land runs no gate (#1872): remove it",
                file=sys.stderr,
            )
    return {key: value for key, value in section.items() if key not in IGNORED.get(name, ())}


def deprecated(name: str, instead: str = "land batches every landable PR (#650)") -> None:
    """Say once per run, on stderr, that a command or setting goes in the next release, and what replaces it: a
    project's own queue keeps working until it adopts the batch `land`."""
    if name not in DEPRECATED:
        DEPRECATED.add(name)
        print(f"deprecated: {name} goes in the next release; {instead}", file=sys.stderr)


def tracker_settings(tracker: str) -> dict[str, dict[str, str]]:
    """docs/agents/issue-tracker.md's settings block, checked against TRACKED: each section's names with their text.
    `components` and `categories` name a label bare (`billing`) or in full (`area:billing`)."""
    data = sections(block(tracker, "issue-tracker.md"), "issue-tracker.md")
    unknown = [name for name in data if name not in TRACKED]
    if unknown:
        raise Refused(f"issue-tracker.md has an unknown settings section `{unknown[0]}`")
    return {
        name: typed(section, TRACKED[name], name, "issue-tracker.md")
        for name, section in data.items()
    }


def delegation(loop: str | None) -> str:
    """loop.md's `delegate`: `all` when the delegated approver's go on a head covers a PR into the default branch
    too, else `feature`, today's rule, where only the owner's does."""
    return str(settings(loop).get("delegate", "feature"))


def delegate_spec(loop: str | None) -> bool:
    """loop.md's `delegate_spec`: whether `delegate: all` covers specs and plans too (default true); false keeps them
    the owner's while the delegate's go on a merge still counts."""
    return settings(loop).get("delegate_spec", True) is not False


def spec_auto(loop: str | None) -> bool:
    """Whether gatekeeper.md's `spec.auto` is true, under which a risk rule holds the spec instead of the merge."""
    return settings(loop).get("spec", {}).get("auto") is True


def spec_kinds(loop: str | None) -> set[str]:
    """The kinds of spec gatekeeper.md's `spec.auto` list names (SPEC_KINDS), which the policy approves; empty when it
    is no list."""
    auto = settings(loop).get("spec", {}).get("auto")
    return {kind for kind in auto if isinstance(kind, str)} if isinstance(auto, list) else set()


def spec_paths(loop: str | None) -> list[str]:
    """The globs of loop.md's `spec.auto` `paths` terms, inside which a spec carrying SCOPED is approved by policy
    (scoped); empty without one."""
    auto = settings(loop).get("spec", {}).get("auto")
    terms = [term for term in auto if isinstance(term, dict)] if isinstance(auto, list) else []
    return [glob for term in terms for glob in term["paths"]]


def matches(terms: dict[str, Any], labels: set[str], paths: list[str] | None) -> str | bool | None:
    """The first term of a rule (`always`, a label, a path glob) that holds on labels and paths, as the gate quotes
    it; False when none does, None when only its paths could and the gate doesn't have them all (the loop judges it).
    The `verifier` term never holds here: ruled() adds the verifier's judgment."""
    if terms.get("always"):
        return "always"
    for label in terms.get("labels", []):
        if label in labels:
            return f"label `{label}`"
    for glob in terms.get("paths", []):
        if any(fnmatch.fnmatch(path, glob) for path in paths or []):
            return f"path `{glob}`"
    return None if paths is None and terms.get("paths") else False


# an incubating feature's PR and its tracking epic carry this label (SKILL.md § Incubating)
INCUBATING = "incubating"
# the body line naming the epic that tracks the feature an incubating PR belongs to
INCUBATES = re.compile(r"^[ \t>*-]*Incubates:[ \t]+#(\d+)\b", re.MULTILINE | re.IGNORECASE)
GRADUATES = re.compile(r"^[ \t>*-]*Graduates:?[ \t]+#(\d+)\b", re.MULTILINE | re.IGNORECASE)
# the first words of the comment that graduates an epic; the epic workflow (incubating-epic.yml) reads them
GRADUATED = "Graduated by"
# the requirements `incubating.waive` may name; an eligible PR stops owing each (incubation)
WAIVABLE = ("evidence", "proof_run", "verifier", "full_gate", "coverage")


def graduation(pull: dict[str, Any], issues: list[dict[str, Any]]) -> list[str]:
    """One line per `incubating` issue the PR would close without a `Graduates #<n>` line for it: an epic stays open
    while the feature incubates, and graduating is the merge of a full-loop PR that says so (gatekeeper.py graduate)."""
    body = pull["body"] or ""
    kinds = links(body)
    said = {int(n) for n in GRADUATES.findall(QUOTED.sub("", body))}
    return [
        f"PR #{pull['number']} would close #{issue['number']}, which is labelled `{INCUBATING}`: an incubating issue"
        f" stays open until a PR graduates it (`Graduates #{issue['number']}`, with the full loop); use `Part of`"
        f" for a slice"
        for issue in issues
        if INCUBATING in names(issue)
        and kinds.get(issue["number"], CLOSES) == CLOSES
        and issue["number"] not in said
    ]


def incubation(
    pull: dict[str, Any], issues: list[dict[str, Any]], loop: str | None
) -> tuple[set[str], list[str]]:
    """The requirements an incubating PR doesn't owe, and a line when its label can't stand. The label is a claim, like
    SCOPED: without an `incubating` policy section it means nothing, and with one it holds only while the PR is eligible
    (ineligible), else the PR owes the full loop. `incubating.waive` names the waived requirements (WAIVABLE; default all)."""
    policy = settings(loop).get(INCUBATING)
    if policy is None or INCUBATING not in names(pull):
        return set(), []
    if GRADUATES.search(QUOTED.sub("", pull["body"] or "")):
        return set(), [
            f"PR #{pull['number']} graduates an issue, so it owes the full loop, not the `{INCUBATING}` waivers"
        ]
    waive = policy.get("waive", list(WAIVABLE))
    if unknown := [name for name in waive if name not in WAIVABLE]:
        raise Refused(
            f"{source(loop)}'s `incubating.waive` names {', '.join(unknown)}; it may name {', '.join(WAIVABLE)}"
        )
    if why := ineligible(pull, issues, loop, policy.get("append_paths", [])):
        return set(), [
            f"PR #{pull['number']} is labelled `{INCUBATING}`, but {why}: it owes the full loop"
        ]
    return set(waive), []


# the variables a gate run takes only from tier()
INCUBATING_ENV = ("GATE_INCUBATING", "GATE_INCUBATING_WAIVES")


def tier(pull: dict[str, Any], loop: str | None) -> list[str]:
    """The variables an eligible incubating PR's gate run takes from this process (gate's `passed`): `GATE_INCUBATING=1`
    and `GATE_INCUBATING_WAIVES`, the waived names (incubation) comma-joined, both set here. The gate command reads them
    and runs less (a project's own task decides what `full_gate`, `proof_run` and `coverage` skip). They sit in the
    command and env of the pass it records, so that pass is keyed apart from a full one and never stands in for it. An
    ineligible or unlabelled PR gets none: its gate runs in full."""
    if INCUBATING not in settings(loop) or INCUBATING not in names(pull):
        return []
    issues = adopted(closing_issues(pull), loop)
    waived, _ = incubation(pull, issues, loop)
    if not waived:
        return []
    os.environ["GATE_INCUBATING"] = "1"
    os.environ["GATE_INCUBATING_WAIVES"] = ",".join(sorted(waived))
    return list(INCUBATING_ENV)


def ineligible(
    pull: dict[str, Any], issues: list[dict[str, Any]], loop: str | None, appendable: list[str]
) -> str | None:
    """Why the PR can't be incubating, or None: its body names an open epic labelled `incubating` (`Incubates: #<n>`);
    every file is added, or modified with no line deleted and matching `append_paths` (a file new features append to);
    and the change doesn't rate high or critical (risk). A deleted or renamed file, a removed line, a file outside the
    list, or a file list the gate can't read in full is existing code touched, which the full loop covers."""
    found = INCUBATES.search(QUOTED.sub("", pull["body"] or ""))
    if not found:
        return "its body has no `Incubates: #<epic>` line"
    try:
        epic = issue_node(int(found[1]))
    except (Refused, subprocess.CalledProcessError):
        return f"epic #{found[1]} can't be read"
    if INCUBATING not in names(epic) or epic.get("state") != "OPEN":
        return f"#{found[1]} is not an open issue labelled `{INCUBATING}`"
    files = pull["files"]
    if (files.get("totalCount") or 0) > len(files["nodes"]):
        return "GitHub can't give every file the PR changes"
    for file in files["nodes"]:
        kind, path = file.get("changeType"), file["path"]
        if kind == "ADDED" or (
            kind == "MODIFIED"
            and file.get("deletions") == 0
            and any(fnmatch.fnmatch(path, glob) for glob in appendable)
        ):
            continue
        return f"it changes existing code (`{path}`, {str(kind).lower()})"
    # risk() drops a term the spec hold answered (`spec.auto`), which would let an added `.github/` file through; the
    # paths a rule names are existing code's boundary here whoever approved the spec, so read them directly
    if (
        why := risk(pull, issues, loop)
        or ruled(names(pull), touched(pull), loop)
        or critical(pull, loop)
    ):
        return f"it rates high or critical ({why})"
    return None


def unowed(
    pull: dict[str, Any], loop: str | None, revert: int | None, waived: set[str]
) -> str | None:
    """Why the PR owes no verifier pass, and so no independent rerun either (rerun_gap, rerun_state), or None: it is
    an exact revert (`revert`, reverts), a `bot` section names it (exempt), or it is an incubating PR whose `waived`
    (incubation) holds `verifier`. One rule, so `check merge` and `check rerun` never disagree on it."""
    if revert is not None:
        return f"it is an exact revert of #{revert}"
    if "verifier" in waived:
        return "it is incubating and waives the verifier"
    if exempt(pull, loop):
        return "a `bot` section of the policy names it"
    return None


def exempt(pull: dict[str, Any], loop: str | None) -> bool:
    """Whether the PR needs no verifier review: a `bot.<name>` section of gatekeeper.md names it (exempting)."""
    return exempting(pull, loop) is not None


def exempting(pull: dict[str, Any], loop: str | None) -> dict[str, Any] | None:
    """The `bot.<name>` section of gatekeeper.md that names the PR and covers every file it changes with one of its
    `paths` (lock and manifest files), or None. A section names a bot by its login, or, with `branch`, a head-branch
    prefix (`renovate/`), for an updater that opens its PRs under a person's token; then the head must be this
    repository's own branch, since a fork's PR may name any branch and only a writer pushes one here. GitHub's GraphQL
    spells a bot's login without the `bot` REST and the setting write, so both compare without it; a person can't
    hold a bot's login, so for a login the author's type is checked too. Every commit's author must be the PR's
    author, and its committer the author or no account (GitHub's web-flow committer on a bot's or an API's commits),
    since another person's push or rebase of its branch is a change no verifier saw. Under a `branch` section an
    author with no account counts as the PR's author too, as a committer does: a cloud worker commits as a git
    identity GitHub ties to no one, and only a writer can push that branch here. A `Co-Authored-By:` trailer names
    no one here: it is text the committer wrote in the message, not another account's push, and GitHub resolves it
    into the commit's `authors` list, so only the commit's own `author` is read. A renamed or copied file refuses
    it, since GraphQL gives only the new path, which may hide code moved onto a lock file's name, and so does the
    label `verify`, which asks for the review (a major update). No entry, or more files or commits than the gate
    reads: no exemption. Under a `branch` section a pack bump whose files are a released tag's byte for byte
    (pin_target) needs neither the commit check nor the move check: a cloud agent commits as an address GitHub may
    tie to a stranger's account (`noreply@anthropic.com` is the user `claude`'s), so who committed proves nothing,
    while the branch (only a writer pushes it) and the release's own bytes do, and the tag proves its moves too."""
    labels = {label["name"] for label in pull["labels"]["nodes"]}
    if "verify" in labels:
        return None
    author, files = pull.get("author") or {}, paths(pull)
    if not files:
        return None
    login = (author.get("login") or "").removesuffix("[bot]").lower()
    if not login:
        return None
    moved = moved_files(pull) != []
    commits = pull.get("authored") or {}
    nodes = commits.get("nodes") or []
    complete = bool(nodes) and (commits.get("totalCount") or 0) <= len(nodes)
    released: list[bool] = []

    def proven(bot: dict[str, Any]) -> bool:
        """Whether a `branch` section's PR is a pin to a released tag byte for byte (pin_target), read once."""
        if "branch" not in bot:
            return False
        if not released:
            released.append(pin_target(pull) is not None)
        return released[0]

    # each commit's author and committer login; None for no GitHub account
    accounts = [
        tuple(
            ((node["commit"].get(role) or {}).get("user") or {}).get("login") or None
            for role in ("author", "committer")
        )
        for node in nodes
    ]

    def by(bot: dict[str, Any]) -> bool:
        """Whether every commit is the PR author's; under a `branch` section an author with no account is too."""
        if not complete:
            return False
        for wrote, committer in accounts:
            if wrote is None and "branch" not in bot:
                return False
            if any(
                name.removesuffix("[bot]").lower() != login
                for name in (wrote, committer)
                if name is not None
            ):
                return False
        return True

    def names(bot: dict[str, Any]) -> bool:
        if "branch" in bot:
            return not pull["isCrossRepository"] and pull["headRefName"].startswith(bot["branch"])
        return (
            author.get("__typename") == "Bot"
            and bot["login"].removesuffix("[bot]").lower() == login
        )

    return next(
        (
            bot
            for bot in settings(loop).get("bot", [])
            if names(bot)
            and all(any(fnmatch.fnmatch(path, glob) for glob in bot["paths"]) for path in files)
            and ((not moved and by(bot)) or proven(bot))
        ),
        None,
    )


# the package a pack bump moves: its `apm.yml` dependency names it, and a bump changes only its `ref:` line
PACK = "kzarzycki/agent-skills/engineering"
# a release the pack tagged: a version or a range of one (`^0.36.0`, `0.36.0`), or the tag (`engineering-v0.36.0`),
# never a branch or a commit, which no release review saw
RELEASE = r"(?:(?:\^|~|>=|=)?v?\d+\.\d+\.\d+|engineering-v\d+\.\d+\.\d+)"
REF_LINE = re.compile(r"^[ \t]*(?:-[ \t]+)?ref:[ \t]*(['\"]?)" + RELEASE + r"\1[ \t]*(?:#.*)?$")
ITEM_LINE = re.compile(r"^([ \t]*)-[ \t]+")
GIT_LINE = re.compile(
    r"^[ \t]*(?:-[ \t]+)?git:[ \t]*['\"]?" + re.escape(PACK) + r"(?:\.git)?['\"]?[ \t]*$"
)


def mechanical(pull: dict[str, Any], loop: str | None) -> str | None:
    """Why the PR is no mechanical pack bump, whose spec then needs no approval, or None when it is one: a `branch`
    section of `bot` exempts it (exempting); it changes the root `apm.yml`, so it moves the pack; and every `apm.yml`
    it changes changes only the `ref:` line of its dependency on PACK (ref_only), so the rest of the diff is the lock
    and generated files the section's `paths` cover. The release such a bump pins was reviewed and tagged already.
    A PR that leaves `apm.yml` alone (an updater's lock-only PR, a hand edit of a generated file), any other
    `apm.yml` change, a login section's PR, or a patch GitHub can't give keeps the spec gate."""
    bot = exempting(pull, loop)
    if bot is None or "branch" not in bot:
        return "no `bot` section with a `branch` exempts it"
    manifests = [path for path in paths(pull) or [] if path.rsplit("/", 1)[-1] == "apm.yml"]
    if "apm.yml" not in manifests:
        return f"it doesn't change `apm.yml`'s `{PACK}` `ref:` line, so it moves no pack"
    changes = changed(pull)
    if changes is None:
        return "GitHub can't give its `apm.yml` patch"
    patches = {change.get("filename"): change.get("patch") for change in changes}
    for path in manifests:
        if not ref_only(patches.get(path) or ""):
            return f"`{path}` changes more than its `{PACK}` `ref:` line"
    return None


def ref_only(patch: str) -> bool:
    """Whether a unified diff of `apm.yml` changes one line, the `ref:` line of the dependency on PACK, to another
    release: one hunk, whose only edits are one REF_LINE removed and then one added, each naming a release (RELEASE)
    and each in a list item with a GIT_LINE naming PACK on its own side of the hunk (pack_item). Any line that is no
    hunk header, context, edit or `\\ No newline` marker fails it, and so does an item whose `git:` line is out of
    the hunk's context."""
    hunks: list[list[str]] = []
    for line in patch.split("\n"):
        if line.startswith("@@"):
            hunks.append([])
        elif hunks and line[:1] in (" ", "-", "+"):
            hunks[-1].append(line)
        elif hunks and line.startswith("\\"):  # \ No newline at end of file
            continue
        elif line:
            return False
    edited = [hunk for hunk in hunks if any(line[:1] in "-+" for line in hunk)]
    if len(edited) != 1:
        return False
    hunk = edited[0]
    edits = [line for line in hunk if line[:1] in "-+"]
    if [line[:1] for line in edits] != ["-", "+"]:
        return False
    if not all(REF_LINE.match(line[1:]) for line in edits):
        return False
    return all(pack_item(hunk, edit, " " + edit[0]) for edit in edits)


def pack_item(hunk: list[str], edit: str, side: str) -> bool:
    """Whether the line `edit` of `hunk` sits in the list item of PACK on its side of the diff (`side`: the context
    and the edits of that side, ` -` old or ` +` new): the item opens at the nearest `- ` line at or above it, runs
    while lines are indented past that dash, and holds a GIT_LINE naming PACK."""
    lines = [line for line in hunk if line[:1] in side]
    text = [line[1:] for line in lines]
    at = lines.index(edit)

    def indent(line: str) -> int:
        return len(line) - len(line.lstrip(" \t"))

    start = at
    while start >= 0 and not ITEM_LINE.match(text[start]):
        start -= 1
    if start < 0:
        return False
    depth = len(ITEM_LINE.match(text[start])[1])
    end = start + 1
    while end < len(text) and text[end].strip() and indent(text[end]) > depth:
        end += 1
    return at < end and any(GIT_LINE.match(line) for line in text[start:end])


REST_FILE_CAP = 3000  # the most files GitHub's REST list of a PR holds


def changed(pull: dict[str, Any]) -> list[dict[str, Any]] | None:
    """The PR's REST file list (pr_changes), read once per PR and kept on the pull; None when GitHub can't give it."""
    if "changes" not in pull:
        try:
            pull["changes"] = pr_changes(pull["number"])
        except (subprocess.CalledProcessError, OSError, ValueError, KeyError):
            return None
    return pull["changes"]


def paths(pull: dict[str, Any]) -> list[str] | None:
    """The files the PR changes. Past the one page of 100 GraphQL gives, the full list comes from REST (changed());
    None when that read fails or GitHub capped it (3000 files, or fewer than the PR's count), so a rule on paths holds
    rather than misses one. A rule on paths reads touched(), which adds a moved file's old name."""
    files = pull["files"]
    found = [file["path"] for file in files["nodes"]]
    total = files.get("totalCount") or 0
    if total <= len(found):
        return found
    changes = changed(pull)
    if changes is None:
        return None
    found = [change["filename"] for change in changes if change.get("filename")]
    return None if len(found) >= REST_FILE_CAP or len(found) < total else found


def moved_files(pull: dict[str, Any]) -> list[str] | None:
    """The renamed or copied files' new paths: GraphQL's nodes when its page holds every file, else the REST entries
    (changed()); None when that list can't be read. The one reader of a move, for touched() and exempt()."""
    files = pull["files"]
    if (files.get("totalCount") or 0) <= len(files["nodes"]):
        return [f["path"] for f in files["nodes"] if f.get("changeType") in ("RENAMED", "COPIED")]
    changes = changed(pull)
    if changes is None:
        return None
    return [c["filename"] for c in changes if c.get("status") in ("renamed", "copied")]


def touched(pull: dict[str, Any]) -> list[str] | None:
    """Every path the PR touches on either side of the change: each file's path (a deleted file's is its old one), and
    a renamed or copied file's old name too (previous), since moving a file across a boundary changes both sides;
    None when the file list can't be read in full (paths()), or when any moved file's old name can't be read, so a rule
    on paths holds rather than misses one. The one place the gate reads renames (#791 reads them here too)."""
    found = paths(pull)
    if found is None:
        return None
    moved = moved_files(pull)
    if moved is None:
        return None
    if moved:
        old = previous(pull)
        # every moved file needs its own old name: a response that lacks one (an entry without it, a file list that
        # changed between the two reads) proves nothing about where it came from
        if old is None or not all(old.get(path) for path in moved):
            return None
        found = found + [old[path] for path in moved if old[path] not in found]
    return found


def previous(pull: dict[str, Any]) -> dict[str, str] | None:
    """Each renamed or copied file's old name by its new one, which GitHub's GraphQL omits, read from the PR's REST
    file list (changed()); None when GitHub can't give the list."""
    changes = changed(pull)
    if changes is None:
        return None
    return {
        change["filename"]: change["previous_filename"]
        for change in changes
        if change.get("filename") and change.get("previous_filename")
    }


def ruled(
    labels: set[str], files: list[str] | None, loop: str | None, judged: str | None = None
) -> str | None:
    """Why gatekeeper.md's `risk` rule holds on these labels and files, or None; paths the gate can't judge for want of the
    files hold, since nothing else would enforce them. `judged` is why the verifier holds the change high (judged()),
    which its `verifier` term reads; None where no verdict is read, such as a spec before the build."""
    terms = settings(loop).get("risk", {})
    hit = matches(terms, labels, files)
    if hit:
        return f"it matches a risk rule ({hit})"
    if hit is None:
        return "the gate can't read every file the PR changes, so the risk rule's `paths` hold until a person judges them"
    if judged and terms.get("verifier"):
        return f"it matches a risk rule (verifier): {judged}"
    return None


def critical_globs(loop: str | None) -> tuple[str, list[str]]:
    """The key naming the critical paths, `critical_paths` or its deprecated name `merge_paths` (settings() refuses
    both), and its globs."""
    terms = settings(loop).get("risk", {})
    key = "merge_paths" if "merge_paths" in terms else "critical_paths"
    return key, terms.get(key, [])


def critical(pull: dict[str, Any], loop: str | None) -> str | None:
    """Why the change rates `critical`, or None: it touches a path `risk.critical_paths` names, whatever its labels,
    `spec.auto` and the verifier's `Risk:` line say. Files the gate can't read hold, as a risk rule's `paths` do."""
    key, globs = critical_globs(loop)
    if not globs:
        return None
    sole = levels(loop)["critical"]["merge"] == "owner"
    files = touched(pull)
    if files is None:
        who = "the owner judges" if sole else "a person judges"
        return (
            f"the gate can't read every file the PR changes, so `risk.{key}` holds until {who} them"
        )
    for glob in globs:
        for path in files:
            if fnmatch.fnmatch(path, glob):
                why = f"it touches `{path}`, which `risk.{key}` names (`{glob}`)"
                return why + ", so only the owner approves its merge" if sole else why
    return None


def levels(loop: str | None) -> dict[str, dict[str, Any]]:
    """What each level (LEVELS) asks of the spec (SPEC_ACTIONS) and the merge (MERGE_ACTIONS), and whether its merge
    needs an independent rerun of the head's selection (`rerun`, rerun_gap): gatekeeper.md's `levels`, each key it
    leaves out at its default, which is the rule before levels existed: `low` {policy, gates}; `high` {policy, go}, or
    under `spec.auto: true` {owner, gates}, since there the owner approved the change before it was built; `critical`
    {policy, owner}; `rerun` false for `low` and true for `high` and `critical` (#2240), since for a high-risk change
    a red the nightly finds is found too late."""
    high: dict[str, Any] = (
        {"spec": "owner", "merge": "gates"}
        if spec_auto(loop)
        else {"spec": "policy", "merge": "go"}
    )
    table: dict[str, dict[str, Any]] = {
        "low": {"spec": "policy", "merge": "gates", "rerun": False},
        "high": {**high, "rerun": True},
        "critical": {"spec": "policy", "merge": "owner", "rerun": True},
    }
    for level, actions in settings(loop).get("levels", {}).items():
        table[level] = {**table[level], **actions}
    return table


def terms(
    pull: dict[str, Any], issues: list[dict[str, Any]], loop: str | None
) -> Iterator[tuple[str, bool, str]]:
    """The risk terms that hold on the change, in the order the gate quotes them, each as (level, after, why): `high`
    for `risk:high` on the PR or an issue it names (or, for a feature branch's merge to the default branch, on a PR
    merged into it), an exact revert of a high-risk change, a risk rule's `always`, `labels` or `paths` (ruled) on the
    labels of the PR and its issues and its files, and its `verifier` term on the verifier's `Risk:` line (judged);
    `critical` for a path `risk.critical_paths` names (critical). `after` marks a term the spec's own hold never reads
    (spec_owned), the label, the revert and the verifier, so it rates the change only after its spec was approved. Lazy, so a
    caller that needs the first term reads no more. An exact revert's issues (gated's `named`) count as its issues."""
    issues = issues + pull.get("named", [])
    marked = [f"PR #{pull['number']}"] * (RISK_LABEL in names(pull)) + [
        f"#{issue['number']}" for issue in issues if RISK_LABEL in names(issue)
    ]
    if not marked and default(pull) and not pull["isCrossRepository"]:
        # a feature branch's merge to the default branch carries every high-risk PR that landed in it
        marked = [
            f"#{n}, merged into {pull['headRefName']}" for n in merged_high(pull["headRefName"])
        ]
    if marked:
        yield "high", True, f"`{RISK_LABEL}` is on {', '.join(marked)}"
    if why := pull.get("reverted"):
        yield "high", True, why
    if why := critical(pull, loop):
        yield "critical", False, why
    labels = names(pull).union(*(names(issue) for issue in issues))
    if why := ruled(labels, touched(pull), loop):
        yield "high", False, why
    if settings(loop).get("risk", {}).get("verifier") and (why := judged(pull, loop)):
        # spec_hold() read the paths and labels before the build; the verdict comes after it
        if spec_auto(loop):
            yield "high", True, f"it matches a risk rule's `verifier` term: {why}"
        else:
            yield "high", True, f"it matches a risk rule (verifier): {why}"


def merge_for(level: str, after: bool, table: dict[str, dict[str, str]]) -> str:
    """What a term at `level` asks of the merge (levels' `table`): the level's `merge`, but `go` for a term read only
    after the spec was approved (`after`) where the level's `spec` is `owner` and its `merge` is `gates`, since the
    owner's spec approval, which stood in for the go, never saw that rating."""
    merge = table[level]["merge"]
    if after and merge == "gates" and table[level]["spec"] == "owner":
        return "go"
    return merge


class Rated(NamedTuple):
    """The change's rating and the merge action it gets (rating): `level`, the highest it rates, and `why`, the first
    term at that level (None for `low`); `merge`, the strictest action any of its terms asks, `low`'s when none holds,
    with `source`, the level of the first term that asks it, and `because`, that term's why."""

    level: str
    why: str | None
    merge: str
    source: str
    because: str

    def line(self, pull: dict[str, Any]) -> str:
        """The line `check merge` and `land` print: the level, why, and the action they applied."""
        rated = f"{self.level} ({self.why})" if self.why else self.level
        return f"PR #{pull['number']}: risk: {rated}; merge: {self.merge}"


def rating(pull: dict[str, Any], issues: list[dict[str, Any]], loop: str | None) -> Rated:
    """The change's level (terms) and what its merge needs (merge_for), the strictest any term asks; a change no term
    raises is `low` and takes `levels.low.merge`."""
    table = levels(loop)
    found = list(terms(pull, issues, loop))
    if not found:
        merge = table["low"]["merge"]
        because = "nothing rates it above low"
        if merge == "owner":
            because += ", and `levels.low.merge` is `owner`"
        return Rated("low", None, merge, "low", because)
    level = max((term[0] for term in found), key=LEVELS.index)
    why = next(term[2] for term in found if term[0] == level)
    asked = [(merge_for(lvl, after, table), lvl, because) for lvl, after, because in found]
    merge = max((ask[0] for ask in asked), key=MERGE_ACTIONS.index)
    _, source, because = next(ask for ask in asked if ask[0] == merge)
    if merge == "owner" and source != "critical":
        because += f", and `levels.{source}.merge` is `owner`"
    return Rated(level, why, merge, source, because)


def owners_only(
    pull: dict[str, Any], loop: str | None, issues: list[dict[str, Any]] | None = None
) -> str | None:
    """Why only the owner's own go on the head lets the PR merge, into any branch, or None: its rating asks
    `owner` of the merge (rating). Where only `critical` may ask it, as by default, the paths alone decide, so no
    issue is read; otherwise the PR's issues are read when the caller has none (rated_issues)."""
    table = levels(loop)
    if all(table[level]["merge"] != "owner" for level in ("low", "high")):
        return critical(pull, loop) if table["critical"]["merge"] == "owner" else None
    found = rating(pull, rated_issues(pull) if issues is None else issues, loop)
    return found.because if found.merge == "owner" else None


def rated_issues(pull: dict[str, Any]) -> list[dict[str, Any]]:
    """The issues the PR's rating reads, as gated() finds them, for a command that judges the merge without gating
    it (approve merge, delegate); an exact revert's go to `named` and its original's risk to `reverted`."""
    revert, _ = reverts(pull)
    if revert:
        pull["named"], pull["reverted"] = closing_issues(pull), reverted(revert)
        return []
    return closing_issues(pull)


def head_passes(
    pull: dict[str, Any], loop: str | None
) -> tuple[str, list[tuple[int, dict[str, str]]]] | str:
    """The commit the verifier's passes judge, the head or the commit whose proofs it carries (carried), and those
    passes' numbers and lines (passes); a string names why none can be read."""
    found = passes(pull)
    if isinstance(found, str):
        return found
    head = pull["headRefOid"].lower()
    on_head = [(number, lines) for commit, number, lines in found if commit == head]
    if not on_head and found and isinstance(carry := carried(pull, loop, found[-1][0]), tuple):
        head = carry[0]
        on_head = [(number, lines) for commit, number, lines in found if commit == head]
    return head, on_head


def judged(pull: dict[str, Any], loop: str | None = None) -> str | None:
    """Why the verifier holds the change high, or None. Every pass after the first reviews only the diff since the last
    reviewed head, so a verifier's risk is the highest `Risk:` of its passes since the last that reviewed the whole
    change: its first pass (by posting order, so a repeated `pass 1` later is a delta), or a later pass whose closing
    `Scope:` line is exactly `full`. A pass whose `Risk:` is any other word than `low`,
    or has no such line, holds; a mixed PR's two verifiers each count. A head no review covers is None, since reviewed()
    then refuses the merge, or the PR needs no review (exempt, an exact revert)."""
    found = head_passes(pull, loop)
    if isinstance(found, str):
        return f"the gate can't read the verifier's `Risk:` line: PR #{pull['number']} {found}"
    if not found[1]:
        return None
    families: dict[str, list[tuple[int, str, dict[str, str]]]] = {}
    for commit, number, lines in passes(pull):  # type: ignore[union-attr]
        families.setdefault(kin(lines["Family"]), []).append((number, commit, lines))
    for family in families.values():
        whole = [
            i
            for i, (number, _, lines) in enumerate(family)
            if i == 0 or lines.get("Scope", "").lower() == "full"
        ]
        for number, commit, lines in family[whole[-1] if whole else 0 :]:
            said = lines.get("Risk")
            if not re.match(r"low\b", said or "", re.IGNORECASE):
                line = "has no `Risk:` line" if said is None else f"says `Risk: {said}`"
                return f"the verifier's pass {number} on {commit[:7]} {line}"
    return None


def risk(pull: dict[str, Any], issues: list[dict[str, Any]], loop: str | None) -> str | None:
    """Why the change rates `high` or `critical` (terms), as the gate shows it with `risk:high` (flag) and as the
    verifier's tier rule reads it, or None: the first term but one the spec's own hold answered, a term read before the
    spec (`after` false) at a level whose `spec` is `owner` and `merge` is `gates`, since there the owner approved the
    change before it was built, and the label, read back as a term after the spec, would ask for a go that approval
    already made needless. By default that is a risk rule's `labels`, `paths` and `always` under `spec.auto: true`."""
    table = levels(loop)
    for level, after, why in terms(pull, issues, loop):
        if merge_for(level, after, table) == merge_for(level, True, table):
            return why
    return None


def reruns(held: str) -> list[tuple[str, str]]:
    """Origin's record branches on tree `held` that an independent rerun made, each as (ref, commit): a record branch
    of any command (RECORD_BRANCH, `<held>-<key>`) whose commit holds `held` as its tree, has one parent, says `Tree
    <held> on <that parent>` on its first line, and whose `Env:` line sets RERUN_VAR, which only `local-ci --record
    --rerun` writes. Who pushed it is rerun_gap's question, not this one's. origin unreadable: none."""
    found = []
    try:
        listed = [
            line.split("\t")
            for line in git("ls-remote", "origin", RECORD_BRANCH.format(f"{held}-*")).splitlines()
        ]
        for commit, ref in listed:
            name = ref.rsplit("/", 1)[1]
            git("fetch", "-q", "--no-tags", "origin", f"+{ref}:refs/loop/records/{name}")
            header, _, message = git("cat-file", "commit", commit).partition("\n\n")
            fields = [line.split(" ", 1) for line in header.splitlines()]
            parents = [value for key, value in fields if key == "parent"]
            lines = message.splitlines()
            env: Any = None
            for line in lines[1:]:
                if line.startswith("Env: "):
                    with contextlib.suppress(ValueError):
                        env = json.loads(line[len("Env: ") :])
            if (
                [value for key, value in fields if key == "tree"] == [held]
                and len(parents) == 1
                and lines
                and lines[0].startswith(f"Tree {held} on {parents[0][:12]}: ")
                and isinstance(env, dict)
                and RERUN_VAR in env
            ):
                found.append((ref, commit.lower()))
    except (subprocess.CalledProcessError, OSError, ValueError):
        return []
    return found


def rerun_gap(pull: dict[str, Any], issues: list[dict[str, Any]], loop: str | None) -> str | None:
    """Why the PR still lacks the independent rerun its level asks (gatekeeper.md's `levels.<level>.rerun`, levels),
    or None. Stated in inputs and results: the head's tree needs a record of its selection run where the worker has no
    control, a rerun record (reruns) that either
    - the forge says the CI's app pushed it (pusher: the newest push to its branch set it to that commit, by the app
      account `loop.rerun_pusher` names, default RERUN_PUSHER), as a CI run on the head pushes it, since no worker's
      own account is that app; or
    - a verifier pass on the head names by its commit on a `Rerun:` line, as the verifier names the record a fresh
      session it started pushed, in a repo with no CI.
    A record branch someone pushed by hand alone is neither, so the worker's own record never satisfies it, nor does a
    rerun record a person's account pushed unless the verifier names it. The level is the rating's (rating), so a
    level whose `rerun` is false (by default `low`) keeps the worker's record as the proof (#2143's D1)."""
    level = rating(pull, issues, loop).level
    if not levels(loop)[level].get("rerun"):
        return None
    where, head = f"PR #{pull['number']}", pull["headRefOid"].lower()
    asked = f"{where} is rated {level}, and `levels.{level}.rerun` asks an independent rerun of its selection on its head:"
    if (held := tree_of(head)) is None:
        return f"{asked} the tree of {head} can't be read here, so no record of it can be"
    found = reruns(held)
    ci_pusher = settings(loop).get("rerun_pusher", RERUN_PUSHER)
    on_head = head_passes(pull, loop)
    named = (
        set()
        # a pass the head carries (head_passes) names a record of the tree it reviewed: a same-tree head's record is
        # that one (R1), and reruns() lists no record of another tree, so a carried name never fits another tree
        if isinstance(on_head, str)
        else {lines.get(RERUN_LINE, "").split(" ")[0].lower() for _, lines in on_head[1]} - {""}
    )
    for ref, commit in found:
        if commit in named:
            return None
    for ref, commit in found:
        try:
            push = pusher(ref)
        except (subprocess.CalledProcessError, OSError, ValueError):
            continue
        if (
            push
            and push.get("after", "").lower() == commit
            and push.get("app")
            and push.get("login") == ci_pusher
        ):
            return None
    said = (
        f"origin holds {len(found)} rerun record(s) on tree {held}, but none {ci_pusher} pushed or a verifier pass on"
        " the head names"
        if found
        else f"origin holds no rerun record on tree {held}"
    )
    return (
        f"{asked} {said}. Where the repo has CI, a CI run on the head runs `local-ci --record --rerun` and pushes its"
        f" record; with no CI, the verifier starts a fresh session that runs it on the head and names the record's"
        f" commit on a `{RERUN_LINE}: <commit>` line of its report"
    )


def rerun_state(pull: dict[str, Any]) -> int:
    """`check rerun <pr>`, a CI job's first step: one line, `PR #<n>: rerun: <state> (rated <level>)`, the state
    `needed` where the rating's level asks a rerun (levels' `rerun`) and rerun_gap finds none yet, `done` where it
    finds one, `not needed (<why>)` where the level asks none or the PR owes no verifier pass (unowed, the rule
    `check merge` applies). Exit 0 whatever the state; it writes nothing."""
    loop = project()[1]
    revert, _ = reverts(pull)
    issues = rated_issues(pull)
    level = rating(pull, issues, loop).level
    waived = incubation(pull, [] if revert else adopting(issues, loop, record=False), loop)[0]
    if why := unowed(pull, loop, revert, waived):
        state = f"not needed ({why})"
    elif not levels(loop)[level].get("rerun"):
        state = f"not needed (`levels.{level}.rerun` is off)"
    else:
        state = "done" if rerun_gap(pull, issues, loop) is None else "needed"
    print(f"PR #{pull['number']}: rerun: {state} (rated {level})")
    return 0


def raises(pull: dict[str, Any], issues: list[dict[str, Any]], loop: str | None) -> str | None:
    """Why flag() adds `risk:high`, or None: the first term (terms) that asks of the merge (merge_for) no less than
    the label itself, read back as a `high` term after the spec, would, since the gate's own label must never ask more
    than the level it shows: not a rule that held the spec at a level on {owner, gates}, nor a `critical` level on
    `gates` beside a `high` one on `go`. By default that is every term risk() shows."""
    table = levels(loop)
    label = MERGE_ACTIONS.index(merge_for("high", True, table))
    for level, after, why in terms(pull, issues, loop):
        if MERGE_ACTIONS.index(merge_for(level, after, table)) >= label:
            return why
    return None


def default(pull: dict[str, Any]) -> bool:
    """Whether the PR's base is its repository's default branch; any other base is a feature branch."""
    return pull["baseRefName"] == pull["baseRepository"]["defaultBranchRef"]["name"]


def signoff(
    pull: dict[str, Any], issue: dict[str, Any], loop: str | None
) -> tuple[bool, str] | None:
    """Whether the owner signs the issue's change off in use after it lands, and why; None when gatekeeper.md has no
    `signoff` section, which leaves every link as it was. The rule is judged as the risk rule is (matches), on the
    labels of the PR and this issue and the PR's files, and paths it can't judge hold; with no term matching, the
    spec's own `Sign-off: owner` line asks for it. A spec's `Sign-off: none` never waives a matching rule, since the
    rule is the owner's standing word."""
    terms = settings(loop).get("signoff")
    if terms is None:
        return None
    hit = matches(terms, names(pull) | names(issue), touched(pull))
    if hit:
        return True, f"it matches a sign-off rule ({hit})"
    if hit is None:
        return (
            True,
            "the gate can't read every file the PR changes, so the sign-off rule's `paths` hold",
        )
    found = SIGNOFF.search(QUOTED.sub("", spec_holder(issue).get("body") or ""))
    if found and found[1].lower() == "owner":
        return True, "its spec says `Sign-off: owner`"
    return False, "no sign-off rule matches" + (
        " and its spec says `Sign-off: none`" if found else ""
    )


def linked(pull: dict[str, Any], issues: list[dict[str, Any]], loop: str | None) -> list[str]:
    """One line per issue the PR links with the wrong keyword for its sign-off (signoff): `Implements #n` when the
    owner signs the change off after landing, so the merge leaves the issue open for it, and a closing keyword
    otherwise, so nothing waits for a sign-off nobody asked for. `Part of` suits both, since the issue stays open
    anyway. An issue GitHub links without a body line closes on merge. Each line names the keyword the PR says
    (worded), the one it needs, and the label, path or spec line that decides it, since a line naming only the
    keyword to use reads as a description of the PR."""
    body = pull["body"] or ""
    kinds, found = links(body), []
    for issue in issues:
        judged = signoff(pull, issue, loop)
        if judged is None or any(label.startswith("wayfinder:") for label in names(issue)):
            continue
        number, kind, (needed, why) = issue["number"], kinds.get(issue["number"], CLOSES), judged
        says = worded(body, number, kind)
        if needed and kind == CLOSES:
            found.append(
                f"#{number}: {says}, but #{number} waits for the owner's sign-off after landing, since {why}:"
                f" use `Implements #{number}`, which leaves it open"
            )
        elif not needed and kind == IMPLEMENTS:
            found.append(
                f"#{number}: {says}, but #{number} needs no sign-off ({why}): use `Closes #{number}`, since"
                " `Implements` would leave it open for a sign-off nobody asked for"
            )
    return found


def worded(body: str, number: int, kind: str) -> str:
    """What the PR body says links issue #number with `kind` (links), the keyword as written (`Fixes #3`), or that
    GitHub links it as closing with no body line."""
    for closes, part, implements, found in CLOSING.findall(QUOTED.sub("", body)):
        word = closes or part or implements
        if (
            int(found) == number
            and (CLOSES if closes else IMPLEMENTS if implements else PART) == kind
        ):
            return f"the PR says `{word.capitalize()} #{number}`"
    return f"GitHub links #{number} to the PR as closing, with no line in its body"


def flag(pull: dict[str, Any], issues: list[dict[str, Any]], loop: str | None) -> list[str]:
    """Add `risk:high` to a high-risk PR that lacks it, so the board shows why it waits: take off every other `risk:*`
    label, since two give the board two answers, and comment the reason risk() gave. A line when GitHub refuses,
    never a failure, since the merge already waits for the owner either way. Added for the verifier's `Risk:` line,
    the comment is FLAGGED, and the label comes off once a later pass judges the risk low (unflag).
    The PR's board `Risk` field (BOARD_RISK) follows (boarded): `High` beside the label. A field already at `High` on a
    PR without the label adds it, naming the field in the comment, since whoever set it made the risk call; a lower
    field never takes the label off, since the label is the merge check's input and only the owner lowers a raise."""
    shown = board_risk(pull["number"])
    if RISK_LABEL in names(pull):
        return unflag(pull, issues, loop) + boarded(pull, loop, shown)
    if (why := raises(pull, issues, loop)) is not None:
        return raise_risk(pull, why, loop) + boarded(pull, loop, shown, high=True)
    if shown[1] != "High":
        return boarded(pull, loop, shown)
    # risk() said low, so the checks after flag() read the label added here to hold the merge
    why = f"the board's `{BOARD_RISK}` field says `High`"
    return raise_risk(pull, why, loop, mark=True)


def raise_risk(pull: dict[str, Any], why: str, loop: str | None, mark: bool = False) -> list[str]:
    """flag()'s raise: add `risk:high` for `why`, take off the other `risk:*` labels and comment the reason; `mark`
    puts the label on the PR as read here too, first, so a refused write still holds this run's merge."""
    where, before = f"PR #{pull['number']}", names(pull)
    if mark:
        pull["labels"] = {"nodes": [{"name": n} for n in sorted(before | {RISK_LABEL})]}
    try:
        label(pull["number"], RISK_LABEL, add=True)
    except subprocess.CalledProcessError as exc:
        return [
            f"{where}: could not add `{RISK_LABEL}` ({(exc.stderr or '').strip() or exc}): add it by hand"
        ]
    found = []
    for other in sorted(n for n in before if n.startswith("risk:")):
        try:
            label(pull["number"], other, add=False)
        except subprocess.CalledProcessError as exc:
            found.append(
                f"{where}: could not remove `{other}` beside `{RISK_LABEL}` ({(exc.stderr or '').strip() or exc}): remove it by hand"
            )
    if (verdict := judged(pull, loop)) and verdict in why:
        body = f"{FLAGGED}, since {why}. A later pass that says `Scope: full` and `Risk: low` takes it off.\n"
        failed = f"{where}: could not comment why the gate added `{RISK_LABEL}`, so it stays until a person removes it"
    else:
        body = f"`{RISK_LABEL}` added, since {why}.\n"
        failed = f"{where}: could not comment why `{RISK_LABEL}` was added ({why})"
    try:
        comment(pull["number"], body)
    except subprocess.CalledProcessError as exc:
        found.append(f"{failed} ({exc})")
    return found


# approvals.py's stand-in module when it ran this source by path and out of sys.modules: the module its importers read
IMPORTER: ModuleType | None = None


def board() -> Any:
    """The `github-tracker` skill's board.py, loaded on first use, since it imports this module: it reads this same one."""
    if "board" not in sys.modules:
        if here := sys.modules.get(__name__) or IMPORTER:
            sys.modules.setdefault("approvals", here)
        folder = installed_scripts("github-tracker")
        if folder is None:
            raise OSError(
                "board.py is the `github-tracker` skill's: install the package that ships it"
            )
        spec = importlib.util.spec_from_file_location("board", folder / "board.py")
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sys.modules["board"] = module
    return sys.modules["board"]


def board_risk(number: int) -> tuple[dict[str, Any] | None, str]:
    """The board's lookup of the PR's `Risk` field (board.py) and the field's option, '' when unset or off the board;
    or None and why the board can't be read: no linked board or field, no `project` token scope, or an API error.
    A workflow's default token can't read a board a personal account owns, so the label alone decides there. The
    board is GitHub's project board, which board.py reads over GraphQL itself, outside the ports."""
    if github._rest_only:
        return None, "GitHub refuses GraphQL here"
    try:
        found = board().lookup(number, BOARD_RISK)
        return found, board().value(found, BOARD_RISK) or ""
    except (ValueError, LookupError, TypeError, OSError, subprocess.CalledProcessError) as exc:
        return None, (getattr(exc, "stderr", "") or "").strip() or str(exc)


def boarded(
    pull: dict[str, Any],
    loop: str | None,
    shown: tuple[dict[str, Any] | None, str],
    high: bool = False,
) -> list[str]:
    """Set the PR's board `Risk` field (board_risk's `shown`) to `High` beside `risk:high` (or `high`, raised this
    run), else to the verifier's `Risk:` word on the head (head_passes), so whoever makes the risk call sets it; no
    pass yet, or the field already at that option, leaves it. Best effort, like a column move: a board that can't
    be read or set is a line, never a failure, since the label is what the merge check reads."""
    if high or RISK_LABEL in names(pull):
        option = "High"
    elif isinstance(found := head_passes(pull, loop), str) or not found[1]:
        return []
    else:
        option = "High" if judged(pull, loop) else "Low"
    repo, said = shown
    if repo is not None and said == option:
        return []
    why = said
    if repo is not None:
        try:
            board().set_field(pull["number"], BOARD_RISK, option, repo)
            return []
        except (ValueError, LookupError, TypeError, OSError, subprocess.CalledProcessError) as exc:
            why = (getattr(exc, "stderr", "") or "").strip() or str(exc)
    # a Low PR has no label, so the verifier's line is what still shows its risk
    shows = f"`{RISK_LABEL}` label" if option == "High" else "verifier's `Risk:` line"
    return [
        (
            f"PR #{pull['number']}: could not set the board's `{BOARD_RISK}` field to `{option}` ({why}); the"
            f" {shows} alone shows the risk"
        )
    ]


def unflaggable(
    pull: dict[str, Any], issues: list[dict[str, Any]], loop: str | None
) -> dict[str, Any] | None:
    """The PR without the `risk:high` the gate added for the verifier's `Risk:` line (FLAGGED), when nothing holds the
    change high without it and unflag would take it off; else None. Reads only, so a check can judge the PR as unflag
    leaves it."""
    flagged = [note["createdAt"] for note in notes(pull) if note["body"].startswith(FLAGGED)]
    added = [
        event["createdAt"]
        for event in pull["timelineItems"]["nodes"]
        if (event.get("label") or {}).get("name") == RISK_LABEL
    ]
    if not flagged or max(added, default="") > max(flagged):
        return None
    unlabelled = {**pull, "labels": {"nodes": [{"name": n} for n in names(pull) - {RISK_LABEL}]}}
    return unlabelled if risk(unlabelled, issues, loop) is None else None


def flagged_as(
    pull: dict[str, Any], issues: list[dict[str, Any]], loop: str | None
) -> dict[str, Any]:
    """The PR as `flag` would leave it, reading only (the board's field included): without the `risk:high` unflag
    would take off (unflaggable), or with the one a board `Risk` field at `High` adds beside a low risk() (flag's
    last case), so a check holds the merge for either as `flag` does in place."""
    if RISK_LABEL in names(pull):
        return unflaggable(pull, issues, loop) or pull
    if risk(pull, issues, loop) is None and board_risk(pull["number"])[1] == "High":
        return {
            **pull,
            "labels": {"nodes": [{"name": n} for n in sorted(names(pull) | {RISK_LABEL})]},
        }
    return pull


def unflag(pull: dict[str, Any], issues: list[dict[str, Any]], loop: str | None) -> list[str]:
    """Take off a `risk:high` the gate added for the verifier's `Risk:` line (FLAGGED) once nothing holds the change
    high without it, since the verifier re-judges a disputed risk. A label added after the gate's comment is a
    person's or the lead's raise, which only the owner lowers."""
    if (unlabelled := unflaggable(pull, issues, loop)) is None:
        return []
    where = f"PR #{pull['number']}"
    try:
        label(pull["number"], RISK_LABEL, add=False)
    except subprocess.CalledProcessError as exc:
        return [f"{where}: could not remove `{RISK_LABEL}` ({(exc.stderr or '').strip() or exc})"]
    pull["labels"] = unlabelled["labels"]
    return [
        f"{where}: removed the `{RISK_LABEL}` the gate added, since the verifier now judges the risk low"
    ]


# the issue field naming the session that holds an issue, which the account's owner creates (claim sets it)
SESSION_FIELD = "Session"
# why this run sets no `Session` field, once a lookup found it missing or unreadable: later claims skip it unasked
unfielded: str | None = None


def session_link() -> str | None:
    """The lead's Omnigent session link, `<server>/c/<session id>`; None outside Omnigent. The lead's id names the
    lead inside its workers and verifiers too, since they run as its children."""
    lead = os.environ.get("OMNIGENT_RUNNER_PRIMARY_SESSION_ID", "").strip()
    server = os.environ.get("RUNNER_SERVER_URL", "").strip().rstrip("/")
    return f"{server}/c/{lead}" if lead and server else None


def issue_field(number: int, name: str) -> tuple[str | None, str]:
    found: tuple[str | None, str] = github.issue_field(number, name)
    return found


def set_issue_field(issue: str, field: str, text: str) -> None:
    github.set_issue_field(issue, field, text)


def rest_has_issue_field(name: str) -> bool:
    found: bool = github.rest_has_issue_field(name)
    return found


def rest_issue_field_id(name: str) -> int | None:
    found: int | None = github.rest_issue_field_id(name)
    return found


def rest_set_issue_field(number: int, field: int, text: str) -> None:
    github.rest_set_issue_field(number, field, text)


# the `Session` field's REST id once a run where GitHub refuses GraphQL found it, so later claims write unasked
rest_field: int | None = None
# what `check setup` and a claim print where the account lacks the field: GitHub's steps to create it
SESSION_STEPS = (
    f"To add it, on GitHub: your profile picture (or the organization) > Settings > Planning > Issue fields > New field;"
    f" Field name `{SESSION_FIELD}`, Field type Text; Create."
)


def session_field(number: int, link: str) -> str | None:
    """Set issue `number`'s `Session` field to `link`: None once set, else the line saying why not. Best effort, like a
    board move, since the claim's record carries the link too. GraphQL reads the field and the issue's id in one call
    and sets it; where GitHub refuses GraphQL, REST checks the field exists, reads its id once a run and sets it. A
    field the account lacks, or one no lookup can read, is said once a run and asked no more (unfielded), so an account
    without the field pays one lookup."""
    global unfielded
    if unfielded is not None or port("tracker") is not github.tracker:
        return None
    missing = f"the account has no `{SESSION_FIELD}` issue field"

    def graphql() -> Callable[[], None] | None:
        field, node = issue_field(number, SESSION_FIELD)
        return None if field is None else lambda: set_issue_field(node, field, link)

    def rest() -> Callable[[], None] | None:
        global rest_field
        if rest_field is None:
            if not rest_has_issue_field(SESSION_FIELD):
                return None
            rest_field = rest_issue_field_id(SESSION_FIELD)
            if rest_field is None:
                raise LookupError(
                    f"GitHub refuses GraphQL here, and REST names the `{SESSION_FIELD}` field's id only once an issue"
                    " carries a value: set it on one issue by hand"
                )
        field = rest_field
        return lambda: rest_set_issue_field(number, field, link)

    try:
        write = github.fast(graphql, rest)
    except (subprocess.CalledProcessError, LookupError, KeyError, TypeError, ValueError) as exc:
        unfielded = (getattr(exc, "stderr", "") or "").strip() or str(exc)
    else:
        if write is None:
            unfielded = missing
        else:
            try:
                write()
                return None
            except subprocess.CalledProcessError as exc:
                why = (exc.stderr or "").strip() or str(exc)
                return f"#{number}: could not set the `{SESSION_FIELD}` field ({why}); its `Assigned:` record names the session"
    said = f"no issue's `{SESSION_FIELD}` field set ({unfielded}); the `Assigned:` record names the session"
    return f"{said}. {SESSION_STEPS}" if unfielded == missing else said


def session_notice() -> list[str]:
    """`check setup`'s note on the optional `Session` issue field: one line and GitHub's steps when the account lacks
    it, a line when GitHub can't say, nothing when it has it. Informational: it changes no exit code."""
    try:
        if rest_has_issue_field(SESSION_FIELD):
            return []
    except (subprocess.CalledProcessError, Refused, OSError) as exc:
        why = (getattr(exc, "stderr", "") or "").strip() or str(exc)
        return [
            f"setup: could not tell whether the account has a `{SESSION_FIELD}` issue field ({why})"
        ]
    return [
        (
            f"setup: the account has no `{SESSION_FIELD}` issue field (optional), so a claim's `Assigned:` record"
            " alone names the lead's Omnigent session."
        ),
        SESSION_STEPS,
    ]


def claim(pull: dict[str, Any], issues: list[dict[str, Any]]) -> list[str]:
    """Building starts: assign each issue no one holds to its spec's approver, with a record that the loop did
    (assigned_by_loop reads it), and give the PR its issues' assignees. The record comes first, so a refused comment leaves no
    assignment assigned_by_loop would read as a person's. A line when GitHub refuses, never a failure, since an assignee says
    who the issue belongs to and gates nothing past `check build`. Under Omnigent the record names the lead's session
    (`Session:`) and so does the issue's `Session` field (session_field)."""
    found, held = [], set()
    link = session_link()
    for issue in issues:
        number, owners = issue["number"], assignees(issue)
        if not owners and (login := approver(issue)):
            details = f"By: loop\nPR: #{pull['number']}" + (f"\nSession: {link}" if link else "")
            try:
                comment(number, fold(f"{ASSIGNED}{login}", details=details))
            except subprocess.CalledProcessError as exc:
                found.append(
                    f"#{number}: did not assign @{login}: could not record the claim ({(exc.stderr or '').strip() or exc})"
                )
                continue
            if link and (said := session_field(number, link)):
                found.append(said)
            try:
                assign(number, login, add=True)
            except subprocess.CalledProcessError as exc:
                found.append(
                    f"#{number}: could not assign @{login} ({(exc.stderr or '').strip() or exc})"
                )
                continue
            owners = {login}
        held |= owners
    for login in sorted(held - assignees(pull)):
        try:
            assign(pull["number"], login, add=True)
        except subprocess.CalledProcessError as exc:
            found.append(
                f"PR #{pull['number']}: could not assign @{login} ({(exc.stderr or '').strip() or exc})"
            )
    return found


def assigned_by_loop(issue: dict[str, Any], login: str) -> bool:
    """Whether the loop's claim() made `login` the issue's assignee: its record names them, and the loop's own
    assignment, the first of them at or after the record, is the last; a later one is a person claiming it again.
    ponytail: when the assignment itself was refused, a person's later assignment reads as the loop's; claim()
    reported that refusal, and removing the approver on parking is undone by assigning them again."""
    recorded = [
        note["createdAt"]
        for note in notes(issue)
        if note["body"].strip().split("\n", 1)[0].strip() == f"{ASSIGNED}{login}"
    ]
    if not recorded:
        return False
    later = [
        event
        for event in events(issue, "AssignedEvent")
        if ((event.get("assignee") or {}).get("login") == login)
        and (event.get("createdAt") or "") >= max(recorded)  # ISO 8601 in UTC, so it orders as text
    ]
    return len(later) <= 1


def graduate(epic: int, pr: int) -> int:
    """Graduate an `incubating` epic: PR #pr is merged and says `Graduates #epic` (graduation() keeps such a PR to the
    full loop). A comment names the PR first, which the epic workflow reads as leave the label alone, then the label
    comes off and the issue closes, in that order, since a closed issue still labelled is reopened."""
    node = issue_node(epic)
    if INCUBATING not in names(node) or node.get("state") != "OPEN":
        raise Refused(f"#{epic} is not an open issue labelled `{INCUBATING}`")
    merged = merged_pull(pr)
    if not merged:
        raise Refused(f"PR #{pr} is not merged, and graduating #{epic} takes its merge")
    if (base := merged.get("base", {}).get("ref")) != default_branch():
        raise Refused(
            f"PR #{pr} merged into {base}, not the default branch: graduating takes the merge into main"
        )
    if epic not in {int(n) for n in GRADUATES.findall(QUOTED.sub("", merged.get("body") or ""))}:
        raise Refused(f"PR #{pr}'s body has no `Graduates #{epic}` line")
    comment(epic, f"{GRADUATED} #{pr}: the incubating label comes off and this epic closes.")
    label(epic, INCUBATING, add=False)
    close_issue(epic)
    print(f"#{epic}: graduated by PR #{pr}")
    return 0


def version(holder: dict[str, Any]) -> str:
    """The issue or comment holding a spec or plan, named by its last edit, as GitHub's edit history shows it."""
    edited = holder.get("lastEditedAt")
    when = (edited or holder["createdAt"]).replace("T", " ").replace("Z", " UTC")
    return f"as edited {when}" if edited else f"as written {when}"


def texted(holder: dict[str, Any]) -> str:
    """The spec or plan named by its text: `sha256 <digest>` of the body as GitHub stores it, line ends made `\\n`.
    An approval names it, so any change to the text voids it wherever the text is read, GraphQL or REST."""
    body = (holder.get("body") or "").replace("\r\n", "\n")
    return f"sha256 {hashlib.sha256(body.encode()).hexdigest()}"


def versions(holder: dict[str, Any]) -> set[str]:
    """The names an approval of this spec or plan may carry: its text's (texted), and, where GraphQL gave its edit
    time, its last edit's (version), which approvals recorded before texts were named carry. REST has no edit time,
    so an approval of the edit alone is unproven there."""
    found = {texted(holder)}
    if "lastEditedAt" in holder:
        found.add(version(holder))
    return found


def last_change(holder: dict[str, Any]) -> str:
    """When the spec or plan last changed, at the latest: its last edit from GraphQL; from REST, which has no edit
    time, its `updatedAt`, which every edit moves (and any other change, so it is never too early)."""
    if "lastEditedAt" in holder:
        return str(holder.get("lastEditedAt") or holder["createdAt"])
    return str(holder.get("updatedAt") or holder["createdAt"])


def names(node: dict[str, Any]) -> set[str]:
    return {label["name"] for label in node["labels"]["nodes"]}


def notes(node: dict[str, Any]) -> list[dict[str, Any]]:
    """Its comments; refused past one page, where an approval or a spec could be among those not read."""
    comments = node["comments"]
    # ponytail: one page of 100; page with `before:` cursors when a spec or PR outgrows it.
    if (comments.get("totalCount") or 0) > len(comments["nodes"]):
        where = f"#{node['number']}"
        raise Refused(
            f"{where} has more than {len(comments['nodes'])} comments: the gate reads one page"
        )
    found: list[dict[str, Any]] = comments["nodes"]
    return found


def bodies(node: dict[str, Any]) -> list[str]:
    return [note["body"] for note in notes(node)]


def headed(
    node: dict[str, Any], heading: str, keep: Callable[[dict[str, Any]], bool] = lambda _note: True
) -> dict[str, Any] | None:
    """The last comment whose first line is `heading`, among those `keep` takes: `## Specification notes` is no
    `## Spec`. A leading `> 🤖 AI` line (`github-tracker`'s github.md) is skipped, so an agent's marked comment is still found."""
    found = [
        note
        for note in notes(node)
        if re.sub(r"\A\s*> 🤖 AI[ \t]*\n\s*", "", note["body"]).strip().split("\n", 1)[0].rstrip()
        == heading
        and keep(note)
    ]
    return found[-1] if found else None


def spec_holder(issue: dict[str, Any]) -> dict[str, Any]:
    """The issue, or its last `## Spec` comment where a tool owns the body."""
    return headed(issue, "## Spec") or issue


def parsed(body: str, point: str) -> dict[str, str] | None:
    """An `Approved: <point>` comment's lower-cased `Key: value` lines; None for any other comment."""
    lines = machine(body).strip().splitlines()
    if lines and lines[0].strip() in VOICED.values():  # the plain line atop a merge record
        lines = lines[1:]
    if not lines or lines[0].strip() != f"Approved: {point}":
        return None
    return {
        key.strip().lower(): value.strip()
        for key, _, value in (line.partition(":") for line in lines[1:])
    }


def records(node: dict[str, Any], point: str) -> list[dict[str, str]]:
    """The approval records for `point` among a node's comments."""
    return [found for body in bodies(node) if (found := parsed(body, point)) is not None]


def current(node: dict[str, Any], point: str, key: str, value: str | set[str], by: str) -> bool:
    """Whether `by` approved `point` for this spec or plan: a record whose `key` is `value`, or one of them."""
    values = {value} if isinstance(value, str) else value
    return any(
        record.get("by") == by and record.get(key) in values for record in records(node, point)
    )


def delegated(pull: dict[str, Any], loop: str | None) -> str | None:
    """Why a delegate's spec or plan record (approve `--by delegate`) can't stand in for the owner's on this PR, or
    None where it does: it counts only while `loop` sets `loop.delegate: all`, no `loop.delegate_spec: false`, and the
    PR touches no path
    `risk.critical_paths` names (critical), which stays the owner's, as for a merge's go (go)."""
    if delegation(loop) != "all":
        return f"{source(loop)} sets no `loop.delegate: all`"
    if not delegate_spec(loop):
        return f"{source(loop)} sets `loop.delegate_spec: false`, which keeps specs and plans the owner's"
    return critical(pull, loop)


def approvals(
    where: str,
    node: dict[str, Any],
    point: str,
    holder: dict[str, Any],
    pull: dict[str, Any] | None = None,
    loop: str | None = None,
) -> list[str]:
    """One line per approval `point` lacks on `node`: the owner's record of this spec or plan (versions), or the
    delegate's where delegated() lets it count for `pull`, and the label. A record of its last edit where the edit
    time can't be read (REST) says how to renew it; a delegate's current record that doesn't count says why."""
    found, held = [], versions(holder)
    owned = current(node, point, point, held, "owner")
    if not owned and pull is not None and current(node, point, point, held, DELEGATE):
        why = delegated(pull, loop)
        owned = why is None
        if why:
            found.append(
                f"{where}: the delegate's `Approved: {point}` record counts for nothing here, since {why}:"
                " the owner approves it"
            )
    if not owned:
        owners = [record for record in records(node, point) if record.get("by") == "owner"]
        edited = "lastEditedAt" not in holder and any(
            str(record.get(point, "")).startswith(("as edited ", "as written "))
            for record in owners
        )
        found.append(
            f"{where} has no `Approved: {point}` record by the owner"
            + (
                f" for its current text: its record names the {point}'s last edit, which GitHub's REST API can't"
                f" date, so the owner approves it again (`approve {point} {node['number']} --by owner`) or the"
                " check runs where GraphQL answers"
                if edited
                else f" for its current {point}: the owner approves it again"
                if owners
                else ""
            )
        )
    if f"approved:{point}" not in names(node):
        found.append(f"{where} lacks the `approved:{point}` label")
    return found


def spec_policy(issue: dict[str, Any], kinds: set[str]) -> bool:
    """Whether a kind `spec.auto` names (`kinds`) approves the issue's spec: `bug`, an issue labelled a bug;
    `follow-up`, one an agent raised (a `Found while #n` line in its body); `sub-issue`, a native sub-issue of an epic
    with the owner's current spec record and `approved:spec`, opened by someone with write access. An epic is never
    covered: the owner approves it."""
    if EPIC in names(issue):
        return False
    if "bug" in kinds and BUG in names(issue):
        return True
    if "follow-up" in kinds and FOUND_WHILE.search(QUOTED.sub("", issue.get("body") or "")):
        return True
    return "sub-issue" in kinds and not parent_gaps(issue)


def parent_gaps(
    issue: dict[str, Any], pull: dict[str, Any] | None = None, loop: str | None = None
) -> list[str]:
    """Why the issue's native parent's approved spec can't stand for its own, one line each; empty when it can: the
    issue is a sub-issue (its `parent`), approvals() finds the parent's spec approved as a direct link's would be (the
    owner's current record, or the delegate's where delegated() lets it count for `pull`, and the label, so a stale
    parent approval stands for nothing), and the issue's author can write to the repo, since anyone who can open an
    issue could otherwise hang their own spec under an approved one. The `sub-issue` kind (spec_policy) and a
    migration row (row_gap) both read it."""
    parent = issue.get("parent")
    if not parent:
        return ["it is no sub-issue"]
    found = approvals(
        f"its parent #{parent['number']}", parent, "spec", spec_holder(parent), pull, loop
    )
    if found:
        return found
    login = (issue.get("author") or {}).get("login")
    if not (login and writer(login)):
        return [f"its author {f'@{login}' if login else '(no account)'} can't write to the repo"]
    return []


def row(issue: dict[str, Any]) -> tuple[int, str] | None:
    """The parent and the row name of the `Part of #<parent> (row <name>)` line the issue's body opens with (ROW), or
    None when it opens with none."""
    found = ROW.match(issue.get("body") or "")
    return (int(found[1]), found[2].strip().strip("\"'`")) if found else None


def row_gap(
    pull: dict[str, Any], issue: dict[str, Any], claimed: tuple[int, str], loop: str | None
) -> str | None:
    """Why the parent's approved spec doesn't cover a migration row's issue, whose body opens with its row line
    (`claimed`, row()), or None when it does: the line names the issue's native parent; neither the issue nor the PR carries
    `risk:high`; the risk rule (ruled, the verifier's `Risk:` line included once a review is read) doesn't hold on
    their labels and every path the PR touches, nor `risk.critical_paths` (critical); and the parent's spec is approved
    for it (parent_gaps). The gate takes the line as the row's claim that the parent's table holds it; the verifier
    checks that claim, and fails a row that adds scope."""
    number, parent = claimed[0], issue.get("parent")
    if not parent:
        return f"it is no sub-issue of #{number}"
    if parent["number"] != number:
        return f"its row line names #{number}, but its parent is #{parent['number']}"
    marked = [f"#{issue['number']}"] * (RISK_LABEL in names(issue))
    if RISK_LABEL in names(pull):  # an unopened branch has no number and no labels
        marked.append(f"PR #{pull['number']}")
    if marked:
        return f"`{RISK_LABEL}` is on {', '.join(marked)}"
    verdict = judged(pull, loop) if "reviews" in pull else None
    if why := ruled(names(issue) | names(pull), touched(pull), loop, verdict):
        return why
    if why := critical(pull, loop):
        return why
    return "; ".join(parent_gaps(issue, pull, loop)) or None


def policy_covers(pull: dict[str, Any], issue: dict[str, Any], loop: str | None) -> bool:
    """Whether `spec.auto` approves the issue's spec: `true` where spec_hold() finds nothing that leaves it to the
    owner, or a kind it names (spec_policy) where the rating doesn't (spec_owned)."""
    return bool(
        spec_auto(loop)
        and spec_hold(pull, issue, loop) is None
        or spec_policy(issue, spec_kinds(loop))
        and spec_owned(pull, issue, loop) is None
    )


def credits(pull: dict[str, Any], issues: list[dict[str, Any]], loop: str | None) -> dict[int, str]:
    """Per issue a migration row's approved parent covers (row_gap), the line `check` says for it, naming the parent
    and the row; an issue `spec.auto` already covers (policy_covers) is not credited, nor is any without a policy."""
    if loop is None:
        return {}
    found = {}
    for issue in issues:
        claimed = row(issue)
        if (
            claimed
            and not any(label.startswith("wayfinder:") for label in names(issue))
            and not policy_covers(pull, issue, loop)
            and row_gap(pull, issue, claimed, loop) is None
        ):
            found[issue["number"]] = (
                f'#{issue["number"]}: covered by #{claimed[0]}\'s approved spec (row "{claimed[1]}")'
            )
    return found


def spec_owned(pull: dict[str, Any], issue: dict[str, Any], loop: str | None) -> str | None:
    """Why the spec's rating leaves it to the owner whatever `spec.auto` says, or None: a level whose `spec` is `owner`
    (levels) that the terms read before the build give it, a risk rule (ruled) on the labels of the issue and the PR
    and the files pushed so far for `high`, `risk.critical_paths` (critical) for `critical`, or `low` when neither
    holds, so a file the spec didn't foresee still reaches the owner. Only a level that asks it is judged. By default
    that is `high` under `spec.auto: true`."""
    table = levels(loop)
    if table["high"]["spec"] == "owner" and (
        why := ruled(names(issue) | names(pull), touched(pull), loop)
    ):
        return why
    if table["critical"]["spec"] == "owner" and (why := critical(pull, loop)):
        return why
    if table["low"]["spec"] != "owner":
        return None
    raised = ruled(names(issue) | names(pull), touched(pull), loop) or critical(pull, loop)
    return None if raised else "nothing rates it above low, and `levels.low.spec` is `owner`"


def spec_hold(pull: dict[str, Any], issue: dict[str, Any], loop: str | None) -> str | None:
    """Why `spec.auto: true` leaves the issue's spec to the owner, or None when the policy approves it: its rating
    (spec_owned), by default a risk rule on the labels of the issue and the PR and the files pushed so far; or an
    author without write access, since anyone who can open an issue would otherwise have their own spec approved."""
    if why := spec_owned(pull, issue, loop):
        return why
    login = (issue.get("author") or {}).get("login")
    if not (login and writer(login)):
        return f"its author {f'@{login}' if login else '(no account)'} can't write to the repo"
    return None


def scoped(pull: dict[str, Any], issue: dict[str, Any], loop: str | None) -> str | None:
    """Why the issue's SCOPED label doesn't approve its spec, or None when loop.md's `spec.auto` `paths` term
    (spec_paths) does: the term is set; neither the issue nor the PR carries `risk:high`; the risk rule, the
    verifier's `Risk:` line included once a review is read, doesn't hold on their labels and every path the PR
    touches (touched); its author can write to the repo; and every path the PR touches, a moved file's old name
    too, matches one of the globs. A file list the gate can't read in full voids it, so the change is never judged
    on part of its files. At `check build` before the PR exists the files are the branch's so far (unopened); the
    merge judges the PR's."""
    globs = spec_paths(loop)
    if not globs:
        return f"`spec.auto` names no `paths`, so `{SCOPED}` approves nothing"
    marked = [f"#{issue['number']}"] * (RISK_LABEL in names(issue))
    if RISK_LABEL in names(pull):  # an unopened branch has no number and no labels
        marked.append(f"PR #{pull['number']}")
    if marked:
        return f"`{RISK_LABEL}` is on {', '.join(marked)}"
    files = touched(pull)
    verdict = judged(pull, loop) if "reviews" in pull else None
    if why := ruled(names(issue) | names(pull), files, loop, verdict):
        return why
    login = (issue.get("author") or {}).get("login")
    if not (login and writer(login)):
        return f"its author {f'@{login}' if login else '(no account)'} can't write to the repo"
    if files is None:
        return "GitHub can't give every file the PR changes, an old name of a moved file included"
    outside = [path for path in files if not any(fnmatch.fnmatch(path, glob) for glob in globs)]
    if outside:
        return f"the PR changes {', '.join(f'`{path}`' for path in outside)}, outside `spec.auto`'s `paths`"
    return None


def holds(pull: dict[str, Any], issues: list[dict[str, Any]], loop: str | None) -> list[str]:
    """A line per issue whose spec `spec.auto: true` leaves to the owner (spec_hold), approved or not, so a gate run says
    why that spec needed them and its merge waits for no `approved:merge`; without it, per issue a kind `spec.auto`
    names would cover but whose rating leaves to the owner (spec_owned). A migration row its approved parent covers
    says so instead (credits). A mechanical pack bump holds none (mechanical)."""
    if issues and loop is not None and "number" in pull and mechanical(pull, loop) is None:
        return []
    credited = credits(pull, issues, loop)
    issues = [issue for issue in issues if issue["number"] not in credited]
    if not spec_auto(loop):
        kinds = spec_kinds(loop)
        return list(credited.values()) + [
            f"#{issue['number']}: its spec is the owner's to approve, though `spec.auto` names its kind, since {why}"
            for issue in issues
            if kinds
            and not any(label.startswith("wayfinder:") for label in names(issue))
            and (why := spec_owned(pull, issue, loop))
            and spec_policy(issue, kinds)
        ]
    return list(credited.values()) + [
        f"#{issue['number']}: its spec is the owner's to approve under `spec.auto: true`, since {why}"
        for issue in issues
        if not any(label.startswith("wayfinder:") for label in names(issue))
        and (why := spec_hold(pull, issue, loop))
    ]


def assignees(node: dict[str, Any]) -> set[str]:
    return {user["login"] for user in (node.get("assignees") or {}).get("nodes", [])}


def events(node: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    """The node's timeline events of `kind` (`LabeledEvent`, `AssignedEvent`), oldest first."""
    found = [
        event
        for event in (node.get("timelineItems") or {}).get("nodes", [])
        if event.get("__typename") == kind
    ]
    return sorted(found, key=lambda event: event.get("createdAt") or "")


def approver(issue: dict[str, Any]) -> str | None:
    """Who approved the issue's spec: the person who last added `approved:spec`, or, for a spec gatekeeper.md's policy
    approves (no label), the account running the loop, whose policy it is. None for a bot, since an agent is never an
    assignee."""
    added = [
        event
        for event in events(issue, "LabeledEvent")
        if (event.get("label") or {}).get("name") == READY
    ]
    if not added:
        return viewer()
    actor = added[-1].get("actor") or {}
    return actor.get("login") if actor.get("__typename") == "User" else None


def others(issue: dict[str, Any]) -> str | None:
    """A line when the issue is assigned to anyone but its spec's approver: an assignee is who the issue belongs to,
    so the loop never builds someone else's. None when it is assigned to no one or to the approver alone."""
    held = assignees(issue)
    if not held:
        return None
    login = approver(issue)
    if held <= {login}:
        return None
    who = ", ".join(f"@{name}" for name in sorted(held))
    return (
        f"#{issue['number']} is assigned to {who}, not to its approver {f'@{login}' if login else '(a person)'}"
        " alone: someone else's issue is theirs, so the loop leaves it"
    )


def closed(pull: dict[str, Any], repo: str, loop: str | None) -> set[int]:
    """The issues of `repo` a PR finishes: GitHub's closing references there and each `#n` after a closing keyword,
    plus `Implements` where gatekeeper.md has a `signoff` rule, which leaves the issue open only for the owner's sign-off.
    `Part of` builds on an issue without finishing it, so two PRs may share one. A reference to another repo's issue
    is a different issue, whatever its number; a body's `#n` is always `repo`'s (CLOSING)."""
    kinds = {CLOSES, IMPLEMENTS} if "signoff" in settings(loop) else {CLOSES}
    named = {n for n, kind in links(pull.get("body") or "").items() if kind in kinds}
    return named | {
        issue["number"]
        for issue in pull["closingIssuesReferences"]["nodes"]
        if in_repo(issue, repo)
    }


def in_repo(issue: dict[str, Any], repo: str) -> bool:
    """Whether a closing reference's issue is `repo`'s: names compare without case, since GitHub spells them either way."""
    return bool(issue["repository"]["nameWithOwner"].lower() == repo.lower())


# how many open PRs duplicates() reads, in one call; a list that long may have left one out
OPEN_LIMIT = 1000


def duplicates(pull: dict[str, Any], loop: str | None) -> list[str]:
    """One line per issue the PR finishes (closed) that another open PR finishes too, naming that PR and its branch:
    two builds of one issue each run a verifier, a gate and the queue, and one of them is thrown away. A list as long
    as OPEN_LIMIT may have left that PR out, so it refuses rather than pass on a partial comparison."""
    repo = pull["baseRepository"]["nameWithOwner"]
    mine = closed(pull, repo, loop)
    if not mine:
        return []
    found = open_pulls()
    if len(found) >= OPEN_LIMIT:
        return [
            (
                f"the duplicate-build check reads at most {OPEN_LIMIT} open PRs and this repo has at least that many,"
                " so it could not see every open PR: close stale PRs, or check by hand that no other open PR closes"
                f" {', '.join(f'#{n}' for n in sorted(mine))}"
            )
        ]
    return [
        f"#{number} is also closed by open PR #{other['number']} (branch `{other['headRefName']}`): one build per"
        " issue, so close one of the PRs, or link it with `Part of`"
        for other in found
        if other["number"] != pull["number"]
        for number in sorted(mine & closed(other, repo, loop))
    ]


def problems(issues: list[dict[str, Any]], kinds: dict[str, set[str]] | None) -> list[str]:
    """One line per reason an issue the PR closes waits for the owner or lacks a label of `kinds` (label_names;
    None, without docs/agents/issue-tracker.md, checks no label's shape); empty when none does. A missing
    `approved:spec` is proofs()'s line."""
    if not issues:
        return ["the PR names no issue: it needs a `Closes #<issue>` line for an approved spec"]
    found = []
    for issue in issues:
        labels = names(issue)
        if NEEDS_OWNER in labels:
            found.append(f"#{issue['number']} waits for the owner ({NEEDS_OWNER})")
        if any(label.startswith("wayfinder:") for label in labels):
            # a wayfinder ticket settles a decision: its wayfinder: label is its category, it has no component or
            # size, and no approval records, so its state is the label alone
            if READY not in labels:
                found.append(f"#{issue['number']} lacks `{READY}`: spec it first")
            continue
        if kinds is None:
            continue
        for kind in ("category", "size"):
            allowed = kinds[kind]
            if len(labels & allowed) != 1:
                found.append(
                    f"#{issue['number']} needs exactly one {kind} label ({', '.join(sorted(allowed))}), has {len(labels & allowed)}"
                )
        if not labels & kinds["component"]:
            found.append(
                f"#{issue['number']} needs a component label ({', '.join(sorted(kinds['component'])) or f'{TRACKER} lists none'})"
            )
    return found


def no_evidence(pull: dict[str, Any]) -> list[str]:
    """A line when the PR's body has no `## Evidence` section with content: `check merge`'s proof and `check ready`'s."""
    if section(pull["body"] or "", "Evidence").strip():
        return []
    return [f"PR #{pull['number']}: its body has no `## Evidence` section with content"]


def demo_form(pull: dict[str, Any]) -> list[str]:
    """A line when the PR's newest `## Demo` comment by a trusted author, not minimized as outdated, links a video or
    screenshot (a release asset, any URL but a user-attachments one) instead of embedding it, or embeds none; a PR
    with no demo has none. A stranger's demo neither holds nor clears the merge."""
    shown = lambda note: not note.get("isMinimized") and trusted(pull, note)
    demo = headed(pull, "## Demo", shown) if "comments" in pull else None
    if demo is None:
        return []
    urls = URL.findall(demo["body"])
    linked = [url for url in urls if not url.startswith(ATTACHMENT)]
    if not linked and urls:
        return []
    what = f"links {linked[0]}" if linked else "embeds no video or screenshot"
    return [
        (
            f"PR #{pull['number']}: its `## Demo` comment {what}: post the demo with the demo skill's"
            " `scripts/demo.sh`, which embeds each video or screenshot as an attachment that plays inline"
        )
    ]


def proofs(
    point: str,
    pull: dict[str, Any],
    issues: list[dict[str, Any]],
    loop: str | None,
    revert: int | None = None,
) -> list[str]:
    """One line per approval or proof the PR and its issues lack at `point` (build or merge); an exact revert of
    #`revert` names no issue and needs no verdict. Without a policy file (`loop` None) an issue needs only the
    `approved:spec` label; with it, the owner's record and the label, unless a kind `spec.auto` names covers it
    (spec_policy), `spec.auto: true` does (spec_hold), a migration row's approved parent does (row_gap), its `paths`
    term does an issue labelled SCOPED (scoped), or the PR is a mechanical pack bump (mechanical).
    A plan needs them where the `plan` rule asks. What the merge
    waits for is waits()'s."""
    rule, found = settings(loop), []
    # a mechanical pack bump's spec needs no approval: the release it pins was reviewed and tagged (mechanical)
    bump = loop is not None and "number" in pull and mechanical(pull, loop) is None
    for issue in issues:
        labels = names(issue)
        if point == "build" and (line := others(issue)):
            found.append(line)
        if any(label.startswith("wayfinder:") for label in labels):
            continue
        where = f"#{issue['number']}"
        if loop is None:
            if READY not in labels:
                found.append(f"{where} lacks the `{READY}` label")
            continue
        # why a row line or `spec:paths` didn't cover the spec: said only beside a missing approval, since the
        # owner's approval covers the spec whatever the row or the paths say
        covered, unrowed, uncovered = bump or policy_covers(pull, issue, loop), None, []
        if not covered and (claimed := row(issue)):
            unrowed = row_gap(pull, issue, claimed, loop)
            covered = unrowed is None
        if not covered and SCOPED in labels:
            why = scoped(pull, issue, loop)
            covered = why is None
            if why:
                uncovered.append(
                    f"{where}: its spec is the owner's to approve, though it carries `{SCOPED}`, since {why}"
                )
        if not covered and unrowed:
            uncovered.append(
                f"{where}: its spec is the owner's to approve, though its body names row"
                f' "{claimed[1]}" of #{claimed[0]}, since {unrowed}'
            )
        if not covered and (
            lacking := approvals(where, issue, "spec", spec_holder(issue), pull, loop)
        ):
            found += uncovered + lacking
        plan = headed(issue, "## Plan")
        if plan is not None:
            if matches(rule.get("plan", {}), labels, None):
                found += approvals(where, issue, "plan", plan, pull, loop)
        elif "plan" in rule.get("practice", {}):
            found.append(f"{where} has no `## Plan` comment, and one is due")
    if (
        point == "build" and "number" in pull
    ):  # before the PR exists (unopened) there is nothing to compare
        found += duplicates(pull, loop)
        # where a lead hands the PR over, so a wrong keyword is fixed before the PR reaches the landing queue
        if loop is not None:
            found += linked(pull, issues, loop)
    waived, lapsed = incubation(pull, issues, loop) if point == "merge" else (set(), [])
    found += lapsed
    if point == "merge":
        found += graduation(pull, issues)
    if "evidence" not in waived:
        found += demo_form(pull)
    if point != "merge":
        return found
    where = f"PR #{pull['number']}"
    if "evidence" not in waived:
        found += no_evidence(pull)
    if loop is not None:
        found += linked(pull, issues, loop)
    if not unowed(pull, loop, revert, waived):
        verdicts = passes(pull)
        found += (unreviewed := reviewed(where, pull, loop, verdicts))
        # only on a verdict that would land, and tier first, since risk() reads the reviews again
        if (
            not unreviewed
            and (tier := tiered(pull, verdicts))
            and (why := risk(pull, issues, loop))
        ):
            found.append(
                f"{where} is high risk ({why}), and its newest verdict came from a lower tier ({tier}): a"
                " default-tier verifier pass on the head first"
            )
        # only on a verdict that would land, since the verifier's pass is one of the two proofs it reads
        if not unreviewed and (line := rerun_gap(pull, issues, loop)):
            found.append(line)
    threads = pull["reviewThreads"]
    if (threads.get("totalCount") or 0) > len(threads["nodes"]):
        found.append(
            f"{where} has more than {len(threads['nodes'])} review threads: the gate reads one page"
        )
    elif unresolved := sum(not thread["isResolved"] for thread in threads["nodes"]):
        found.append(
            f"{where}: {unresolved} review thread(s) unresolved: fix and reply, or triage, then resolve (github-tracker's github.md, Review trail)"
        )
    found += [
        f"{where}: {(review.get('author') or {}).get('login', 'a reviewer')} requested changes: answer the review"
        for review in pull["latestReviews"]["nodes"]
        if review["state"] == "CHANGES_REQUESTED"
    ]
    return found


def passes(pull: dict[str, Any]) -> list[tuple[str, int, dict[str, str]]] | str:
    """The verifier's PR reviews in order, each as its commit, pass number and `VERDICT:`/`SATISFIED:`/`Risk:`/`Scope:`/`Logic change:` lines (`Scope:` read from the closing block only,
    since a criteria line may say anything): a body
    starting `Verifier (<family>), pass <n>`, by the PR's author, the account running this or a writer (writer), since
    anyone can review a public repo, and a colleague (or their agent) verifying someone else's PR posts as themself.
    A verifier's newest review on a commit and pass replaces its earlier ones there (kin), since one that parked on a
    question rewrites its verdict in the same pass; a mixed PR's two families both stay. A string names why none can
    be read."""
    reviews = pull["reviews"]
    if (reviews.get("totalCount") or 0) > len(reviews["nodes"]):
        return f"has more than {len(reviews['nodes'])} reviews: the gate reads one page"
    found = [review for review in reviews["nodes"] if PASS.match(review.get("body") or "")]
    # the lines read, plus the heading's family, which names the verifier's tier when it ran below the default
    trusted = {(pull.get("author") or {}).get("login"), viewer() if found else None} - {None}
    others = {(review.get("author") or {}).get("login") for review in found} - trusted - {None}
    trusted |= {login for login in others if writer(login)}
    read = [
        (
            ((review.get("commit") or {}).get("oid") or "").lower(),
            int(PASS.match(review["body"])[2]),
            {
                "Family": PASS.match(review["body"])[1],
                **{
                    key.strip(): value.strip()
                    for key, _, value in (
                        line.partition(":") for line in review["body"].splitlines()
                    )
                    if key.strip() in ("VERDICT", "SATISFIED", "Risk", LOGIC, RERUN_LINE)
                },
                **{
                    key.strip(): value.strip()
                    for key, _, value in (
                        line.partition(":")
                        for line in re.split(
                            r"\r?\n\r?\n", folding.visible(review["body"]).rstrip()
                        )[-1].splitlines()
                    )
                    if key.strip() == "Scope"
                },
            },
        )
        for review in found
        if (review.get("author") or {}).get("login") in trusted
    ]
    keys = [(commit, number, kin(lines["Family"])) for commit, number, lines in read]
    return [entry for i, entry in enumerate(read) if keys[i] not in keys[i + 1 :]]


def kin(family: str) -> str:
    """A review heading's verifier family: its harness word (`codex/terra-6.1/med mid` is `codex`), with Claude Code's
    `cc` read as `claude`, so one verifier's reviews group whatever model, tier or qualifier each names. ponytail: two
    verifiers of one harness in one pass count as one; key on the model too if a PR ever runs two such."""
    word = re.match(r"[^/,\s]*", family)[0].lower()  # type: ignore[index]
    return "claude" if word == "cc" else word


def lower(verdicts: list[tuple[str, int, dict[str, str]]] | str) -> str | None:
    """The tier below the default (LOWER_TIER) the newest verifier pass names in its heading's family, the newest
    commit's highest pass (a mixed PR's two verifiers share one), or None; a fallback family (`agy`, `gemini`,
    `same-family`) names no tier and counts as the default, as does a heading naming none, so verdicts written before tiers were named keep landing. `verdicts` is passes()'s."""
    if isinstance(verdicts, str) or not verdicts:
        return None
    head = verdicts[-1][0]
    on_head = [(number, lines) for commit, number, lines in verdicts if commit == head]
    last = max(number for number, _ in on_head)
    tiers = [
        found[1]
        for number, lines in on_head
        if number == last and (found := LOWER_TIER.search(lines["Family"]))
    ]
    return tiers[0].lower() if tiers else None


# A PR that only bumps the engineering pack's pin (#1218): the pack's repository and release tags, the pin files apm
# reads, and a skill file apm vendors (`.agents/skills/<skill>/...`, `.claude/skills/<skill>/...`) from the pack's
# `<virtual_path>/skills/`. The tag's own copy of the rule sits in one of SCRIPTS, the gatekeeper's file or, before
# the split, approvals.py's.
PACK_REPO = "kzarzycki/agent-skills"
PACK_TAG = re.compile(r"engineering-v(\d+)\.(\d+)\.(\d+)")
COMMIT_SHA = re.compile(r"[0-9a-f]{40}", re.IGNORECASE)
PIN_FILES = ("apm.yml", "apm.lock.yaml")
ANNOTATIONS = __import__("__future__").annotations.compiler_flag
VENDORED = re.compile(r"\.[^/]+/skills/(.+)")
RULE_FILES = (
    "gatekeeper/skills/gatekeeper/scripts/gatekeeper.py",
    *(
        f"engineering/skills/engineering-loop/scripts/{name}"
        for name in ("gatekeeper.py", "approvals.py")
    ),
)


def tiered(
    pull: dict[str, Any], verdicts: list[tuple[str, int, dict[str, str]]] | str
) -> str | None:
    """lower() of the verdicts by the rule that judges this PR: the pin target's, read from its tag (pinned_rule), on
    a PR that only bumps the pack to a released tag (pin_target), since the base's rule there is the one the pin
    retires; main's otherwise, and wherever the tag's rule can't be read or fails."""
    if isinstance(verdicts, str) or not verdicts or (tag := pin_target(pull)) is None:
        return lower(verdicts)
    rule = pinned_rule(tag)
    if rule is None:
        return lower(verdicts)
    try:
        found = rule(verdicts)
    except Exception:  # noqa: BLE001 - a rule the tag ships that fails on these verdicts is no rule: main's judges
        return lower(verdicts)
    return found if found is None or isinstance(found, str) else lower(verdicts)


def pin_target(pull: dict[str, Any]) -> str | None:
    """The released pack tag (PACK_TAG of PACK_REPO) the PR pins, when its diff touches only the pin (PIN_FILES) and
    the pack's vendored skill files, and every vendored file in it equals the tag's byte for byte (by blob SHA, a hash
    of the bytes; a removed one is one the tag lacks, and one main's own lock lists), and so does every file the head's
    lock deploys, in the diff or not, with every skill the tag ships deployed and all its files present (deployed_is_tag); else None. The lock at the head names the target: its pack
    dependency's `resolved_ref`, whose `resolved_commit` must be the tag's commit, with apm.yml pinning that ref; a
    `resolved_ref` that is a full commit SHA names the release tag at that commit (sha_tag); a
    vendored file is one either side's lock lists among the pack's `deployed_files`. The tag is read from the pack's
    repository, never from the PR's tree, which its author controls. Any read that fails, or a file list past the
    one REST gives: None, so main's rule judges, as before."""
    shaped = touched(pull)
    if shaped is not None and (
        not shaped or any(path not in PIN_FILES and not VENDORED.fullmatch(path) for path in shaped)
    ):
        return None  # most PRs, and one changing nothing: no call beyond the PR node
    try:
        changes = pr_changes(pull["number"])
        total = pull["files"].get("totalCount") or 0
        head = pinned_pack(file_at(pull["headRefOid"], "apm.lock.yaml"))
        if head is None or total > len(changes):
            return None
        ref, commit, virtual, deployed = head
        tag = sha_tag(ref) if COMMIT_SHA.fullmatch(ref) else ref
        if tag is None or not PACK_TAG.fullmatch(tag):
            return None
        if not pins(file_at(pull["headRefOid"], "apm.yml"), ref):
            return None
        base = pinned_pack(file_at(pull["baseRefOid"], "apm.lock.yaml"))
        released = tag_tree(PACK_REPO, tag)
        placed = tree(
            pull["headRefOid"]
        )  # one read of the head's tree, for deployed_is_tag() below
    except (
        subprocess.CalledProcessError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        yaml.YAMLError,
    ):
        return None
    if released is None or placed is None or released[0].lower() != str(commit).lower():
        return None
    files = released[1]
    if not deployed_is_tag(deployed, virtual, files, placed):
        return None
    owned = set(base[3] if base else ())  # main's lock alone controls what a PR may remove
    vendored = set(deployed) | owned

    def source(path: str, removed: bool = False) -> str | None:
        found = VENDORED.fullmatch(path)
        return (
            f"{virtual}/skills/{found[1]}"
            if found and path in (owned if removed else vendored)
            else None
        )

    for change in changes:
        path, status = change["filename"], change.get("status")
        # a rename removes its old path, which must be a vendored file the tag lacks (a pin file is none)
        old = change.get("previous_filename")
        if old and status == "renamed" and ((was := source(old, True)) is None or was in files):
            return None
        if path in PIN_FILES:
            continue
        if (want := source(path, status == "removed")) is None:
            return None
        if status == "removed":
            if want in files:
                return None
        elif want not in files or files[want][1] != change.get("sha"):
            return None
    return tag


def deployed_is_tag(
    deployed: list[str],
    virtual: str,
    tagged: dict[str, tuple[str, str]],
    placed: dict[str, tuple[str, str]],
) -> bool:
    """Whether the head's deployed pack files, in the diff or not, are the tag's: every file under a `deployed_files`
    entry that maps to the pack (VENDORED) is in the tag at the same blob SHA, each such entry names a file in the
    head's tree, and every file the tag ships for those skills is in the head's tree under the entry's root and
    covered by an entry (a skill the tag ships that no entry deploys fails). `tagged` is tag_tree()'s files, `placed` tree()'s. An entry outside the pack's skills is
    none of its files and is skipped."""
    entries = [(d, found) for d in deployed if (found := VENDORED.fullmatch(d))]
    roots: dict[
        str, set[str]
    ] = {}  # skill -> the roots (`.claude`, `.agents`) the lock deploys it under
    for entry, found in entries:
        root = entry[: found.start(1) - len("/skills/")]
        roots.setdefault(found[1].split("/")[0], set()).add(root)
        here = [p for p in placed if p == entry or p.startswith(f"{entry}/")]
        if not here:
            return False
        for path in here:
            want = f"{virtual}/skills/{path[len(root) + len('/skills/') :]}"
            if want not in tagged or tagged[want][1] != placed[path][1]:
                return False
    for want in tagged:
        skill, _, rest = want.removeprefix(f"{virtual}/skills/").partition("/")
        if not want.startswith(f"{virtual}/skills/") or not rest:
            continue
        if skill not in roots:  # a skill the tag ships and the lock does not deploy
            return False
        for root in roots[skill]:
            path = f"{root}/skills/{skill}/{rest}"
            covered = any(path == d or path.startswith(f"{d}/") for d, _ in entries)
            if path not in placed or not covered:
                return False
    return True


def sha_tag(sha: str) -> str | None:
    """The release tag (PACK_TAG) of PACK_REPO whose commit is `sha`, the highest version when several are, or None
    when none is: one read of the pack's tag list."""
    found = [
        (tuple(int(part) for part in match.groups()), name)
        for name, commit in repo_tags(PACK_REPO).items()
        if (match := PACK_TAG.fullmatch(name)) and commit.lower() == sha.lower()
    ]
    return max(found)[1] if found else None


def pinned_pack(lock: str | None) -> tuple[str, str, str, list[str]] | None:
    """The pack's (resolved_ref, resolved_commit, virtual_path, deployed_files) in an apm.lock.yaml's text, or None
    when it has no dependency on the pack from GitHub."""
    data = yaml.safe_load(lock) if lock else None
    if data is not None and not isinstance(data, dict):
        raise ValueError("apm.lock.yaml is not a mapping")
    for dep in (data or {}).get("dependencies") or []:
        if (
            isinstance(dep, dict)
            and dep.get("repo_url") == PACK_REPO
            and dep.get("host", "github.com") == "github.com"
            and dep.get("virtual_path") == "engineering"
        ):
            ref, commit = dep.get("resolved_ref"), dep.get("resolved_commit")
            if isinstance(ref, str) and isinstance(commit, str):
                return (
                    ref,
                    commit,
                    "engineering",
                    [str(path) for path in dep.get("deployed_files") or []],
                )
    return None


def pins(manifest: str | None, tag: str) -> bool:
    """Whether an apm.yml's text pins the pack at `tag`: `{git: <repo>/engineering, ref: <tag>}` or
    `<repo>/engineering#<tag>` among its apm dependencies."""
    data = yaml.safe_load(manifest) if manifest else None
    deps = data.get("dependencies") if isinstance(data, dict) else data
    if deps is not None and not isinstance(deps, dict):
        raise ValueError("apm.yml is not a mapping")
    entries = (deps or {}).get("apm") or []
    want = f"{PACK_REPO}/engineering"
    return any(
        entry == f"{want}#{tag}"
        or (isinstance(entry, dict) and entry.get("git") == want and entry.get("ref") == tag)
        for entry in entries
    )


def pinned_rule(tag: str) -> Callable[[Any], Any] | None:
    """The tag's lower(), read from the pack's repository at `tag` (RULE_FILES, the first that defines it); None when
    no file there defines it, so main's rule applies. Only that function runs, with the module-level names it reads,
    each an assignment of the tag's file: the tier rule is a pure function of the verdicts, so no other code of the
    tag (its I/O, its other checks) can change how this run judges."""
    for path in RULE_FILES:
        try:
            text = file_at(tag, path, PACK_REPO)
        except (subprocess.CalledProcessError, OSError):
            return None
        if text and (rule := rule_in(text)):
            return rule
    return None


def rule_in(text: str) -> Callable[[Any], Any] | None:
    """lower() of a gatekeeper source's text, built with only the module-level assignments it reads (and theirs); None
    when the text defines no lower() of one argument or can't be built."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    defined = {
        node.targets[0].id: node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
    }
    rule = next(
        (node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "lower"),
        None,
    )
    if rule is None or len(rule.args.args) != 1:
        return None
    pending: list[ast.AST] = [rule]
    seen: set[str] = set()
    while pending:
        for node in ast.walk(pending.pop()):
            if isinstance(node, ast.Name) and node.id in defined and node.id not in seen:
                seen.add(node.id)
                pending.append(defined[node.id])
    # in the file's order, so a name is assigned before one that reads it
    needed = [node for name, node in defined.items() if name in seen] + [rule]
    space: dict[str, Any] = {"re": re, "Any": Any}
    code = ast.Module(body=needed, type_ignores=[])
    try:
        # annotations stay unevaluated, as under the file's own `from __future__ import annotations`
        exec(  # noqa: S102 - a released tag's own tier rule, the pack's code as main's copy is
            compile(code, f"<{PACK_REPO} lower>", "exec", flags=ANNOTATIONS, dont_inherit=True),
            space,
        )
    except Exception:  # noqa: BLE001 - a file shape this can't rebuild: main's rule
        return None
    return space["lower"]


def counts(lines: dict[str, str]) -> tuple[int, int] | None:
    """A verdict's blocker and major counts; None when its `VERDICT:` line lacks one."""
    found = [
        re.search(rf"(\d+)\s+{kind}s?\b", lines.get("VERDICT", "")) for kind in ("blocker", "major")
    ]
    return None if None in found else (int(found[0][1]), int(found[1][1]))


def held(lines: dict[str, str]) -> bool:
    """Whether a verdict is satisfied with no blocker or major open."""
    return lines.get("SATISFIED", "").lower().split()[:1] == ["yes"] and counts(lines) == (0, 0)


def cap(loop: str | None) -> int:
    """The project's cap: gatekeeper.md's `cap` setting, else this skill's SKILL.md's default."""
    if (own := settings(loop).get("cap")) is not None:
        return own
    skill = Path(__file__).resolve().parents[1] / "SKILL.md"
    found = CAP_DEFAULT.search(skill.read_text())
    if found is None:
        raise Refused(
            "the gatekeeper skill's SKILL.md § The cap names no default (`The cap is <n> passes`)"
        )
    return int(found[1])


def reviewed(
    where: str,
    pull: dict[str, Any],
    loop: str | None,
    verdicts: list[tuple[str, int, dict[str, str]]] | str | None = None,
) -> list[str]:
    """One line per reason the verifier's PR reviews (passes) let nothing land: the newest pass is not on the head,
    since every commit after a verdict needs one of its own, unless the head carries that commit's proofs (carried: on
    the same tree, or where loop.md opts in, which prints a line either way), or on its commit the highest pass (a mixed PR's two
    verifiers share one, and a later pass on the same commit supersedes an earlier one) is not satisfied, or has a
    blocker or a major open; majors with no blocker at the cap or later pass once the PR body records the lead's
    decision (CAP_DECISION). A triage or comment is no verdict. An allow-listed bot's lock- or manifest-only PR
    needs none (exempt). `verdicts` is passes()'s, when the caller has read it."""
    verdicts = passes(pull) if verdicts is None else verdicts
    if isinstance(verdicts, str):
        return [f"{where} {verdicts}"]
    if not verdicts:
        return [f"{where}: no verifier review posted (approvals.py verdict)"]
    head = verdicts[-1][0]
    on_head = [(number, lines) for commit, number, lines in verdicts if commit == head]
    last = max(number for number, _ in on_head)
    same = [lines for number, lines in on_head if number == last]
    if not head or any(counts(lines) is None or "SATISFIED" not in lines for lines in same):
        return [
            f"{where}: a verifier review lacks its commit, or a `VERDICT:` or `SATISFIED:` line (approvals.py verdict)"
        ]
    pairs = [pair for pair in map(counts, same) if pair is not None]
    blocker, major = sum(pair[0] for pair in pairs), sum(pair[1] for pair in pairs)
    # at the cap the lead decides: majors filed as a follow-up (CAP_DECISION) land on this verdict; a blocker never does
    decided = (
        not blocker
        and major
        and last >= cap(loop)
        and CAP_DECISION.search(QUOTED.sub("", pull.get("body") or "")) is not None
    )
    found = []
    if pull["headRefOid"].lower() != head and not carries(where, pull, loop, head):
        found.append(
            f"{where}: the newest verifier review is on {head}, not on the head {pull['headRefOid']}:"
            " every commit after a verdict needs a verdict of its own"
        )
    if decided:
        return found
    if any(lines["SATISFIED"].lower().split()[:1] != ["yes"] for lines in same):
        found.append(f"{where}: a verifier review on {head} is not satisfied: fix and verify again")
    if blocker or major:
        found.append(
            f"{where}: the verifier reviews on {head} have {blocker} blocker and {major} major open: fix and verify again"
        )
    return found


def waits(
    pull: dict[str, Any],
    issues: list[dict[str, Any]],
    loop: str | None,
    pushed: Callable[[dict[str, Any]], str | None] | None = None,
    checks: list[str] | None = None,
) -> list[str]:
    """One line per thing the merge waits for: ran()'s checks (`checks`, when the caller read them), and the owner's go
    (owner_waits)."""
    return (ran(pull) if checks is None else checks) + owner_waits(pull, issues, loop, pushed)


def no_ci(loop: str | None) -> bool:
    return settings(loop).get("ci") is False


def opted_out(loop: str | None) -> bool:
    return settings(loop).get("opt_out", False)


# the variable Claude Code sets to `true` in a cloud session's VM, where the checkout's signing key is the session's own
CLOUD = "CLAUDE_CODE_REMOTE"
# the config every git command takes during a cloud land (placed): sign and verify a squash with the landing key, through
# ssh-keygen itself rather than the VM's signing program, which signs with the session's key
SIGNING: list[str] = []


# a signed land runs ssh-keygen to read the key and to sign and verify each squash
NO_KEYGEN = "land with `land.signed_squash_to` needs `ssh-keygen` (the OpenSSH client), which is not installed: install it"


def in_cloud() -> bool:
    return os.environ.get(CLOUD) == "true"


def listed(public: str, signers: Path) -> bool:
    """Whether the public key `public` (`<type> <base64> [comment]`) is a line of the allowed signers file."""
    key = public.split()[:2]
    lines = signers.read_text().splitlines()
    return len(key) == 2 and any(
        fields[i : i + 2] == key
        for line in lines
        if not line.lstrip().startswith("#")
        for fields in [line.split()]
        for i in range(len(fields) - 1)
    )


def armoured(value: str) -> str:
    """A signing key as ssh-keygen reads it: literal `\\n` as newlines, and a value with no `-----BEGIN` line, which
    a one-line environment variable leaves, as its base64 in OpenSSH armour at 70 characters a line."""
    value = value.replace("\\n", "\n")
    if "-----BEGIN" in value:
        return value
    body = "".join(value.split())
    lines = [body[i : i + 70] for i in range(0, len(body), 70)]
    return "\n".join(
        # pragma: allowlist nextline secret
        ["-----BEGIN OPENSSH PRIVATE KEY-----", *lines, "-----END OPENSSH PRIVATE KEY-----"]
    )


@contextlib.contextmanager
def placed(loop: str | None, queue: bool = False) -> Any:
    """Refuse a land where loop.md's `land.where` (default `both`) excludes the place it runs, before any PR is read.
    A cloud session (CLOUD) on a base without GitHub's merge queue (`queue`, which merges and signs each PR itself) and
    without `land.signed_squash_to` holds no key: GitHub's merge, over REST there, signs with GitHub's key. With
    `land.signed_squash_to` refuse a land with no `ssh-keygen` installed, and one whose `signing_key_env` variable is
    unset, empty or holds a key the allowed signers lack; else hold that key (armoured(), if a one-line variable stripped its armour) in a mode-0600 file in
    a fresh temporary directory, removed whatever happens, and sign each squash with it (SIGNING). The key and the variable's value are never printed."""
    global SIGNING
    land, cloud = settings(loop).get("land"), in_cloud()
    place = "a cloud session" if cloud else "a host"
    where = (land or {}).get("where", "both")
    where = LAND_WHERE.aliases.get(where, where)
    if where not in ("both", "cloud" if cloud else "host"):
        raise Refused(f"loop.md's `land.where` is `{where}`, so land does not run in {place}")
    # a cloud land without `signed_squash_to` merges over REST, which GitHub signs: no key is held
    if not cloud or queue or not (land or {}).get("signed_squash_to"):
        yield
        return
    if not shutil.which("ssh-keygen"):
        raise Refused(NO_KEYGEN)
    name = land.get("signing_key_env")
    if not name:
        raise Refused(
            "land in a cloud session signs with the key in the variable loop.md's `land.signing_key_env` names,"
            " and it names none"
        )
    key = os.environ.get(name)
    if key is None:
        raise Refused(f"the variable {name} (loop.md's `land.signing_key_env`) is not set")
    if not key.strip():
        raise Refused(f"the variable {name} (loop.md's `land.signing_key_env`) is empty")
    signers = Path(land["allowed_signers"]).expanduser()
    if not signers.is_file():
        raise Refused(f"no allowed signers file at {signers}")
    folder = Path(tempfile.mkdtemp(prefix="land-key-"))
    try:
        file, key = folder / "key", armoured(key)
        with os.fdopen(os.open(file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as out:
            out.write(key if key.endswith("\n") else key + "\n")
        public = subprocess.run(
            ["ssh-keygen", "-y", "-P", "", "-f", str(file)],
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
        )
        # ssh-keygen's error is not repeated: it is about the key, which stays unprinted
        if public.returncode:
            raise Refused(
                f"the variable {name} (loop.md's `land.signing_key_env`) holds no SSH private key without a"
                " passphrase"
            )
        if not listed(public.stdout, signers):
            raise Refused(
                f"the key in the variable {name} (loop.md's `land.signing_key_env`) is not in {signers}: add its"
                " public key there"
            )
        SIGNING = [
            *("-c", "gpg.format=ssh"),
            *("-c", f"user.signingkey={file}"),
            *("-c", "gpg.ssh.program=ssh-keygen"),
        ]
        try:
            yield
        finally:
            SIGNING = []
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def signed(loop: str | None) -> tuple[str, Path] | None:
    """loop.md's `land` section as (branch, signers), None without one."""
    land = settings(loop).get("land") or {}
    if "signed_squash_to" not in land:
        return None
    return (land["signed_squash_to"], Path(land["allowed_signers"]).expanduser())


def merge_method(pull: dict[str, Any], loop: str | None) -> str:
    """How land merges the PR: `merge` (a merge commit, which keeps the head's ancestry) when its head branch matches
    a pattern of loop.md's `land.merge_commit_heads`, else `squash`. A match with `land.signed_squash_to` set is
    Refused, since a signed squash has no merge-commit form."""
    patterns = (settings(loop).get("land") or {}).get("merge_commit_heads", [])
    head = pull["headRefName"]
    if not any(fnmatch.fnmatchcase(head, pattern) for pattern in patterns):
        return "squash"
    if signed(loop):
        raise Refused(
            f"PR #{pull['number']}'s head `{head}` matches `land.merge_commit_heads`, but `land.signed_squash_to` is"
            " set, and a signed squash has no merge-commit form: unset one of the two keys"
        )
    return "merge"


def unsignable(pull: dict[str, Any], branch: str, signers: Path) -> str | None:
    """Why this checkout can't land the PR as a signed squash, checked before the PR is touched."""
    where = f"PR #{pull['number']}"
    if pull["isCrossRepository"]:
        return f"{where} comes from another repository: its branch isn't ours to push, push it to {pull['baseRepository']['nameWithOwner']} first"
    if pull["baseRefName"] != branch:
        return f"{where} targets {pull['baseRefName']}, but the policy lands signed squashes only to {branch}"
    if pull["autoMergeRequest"] is not None or pull["isInMergeQueue"]:
        return f"{where}: auto-merge or the merge queue would merge it with GitHub's signature: turn it off"
    # REST can't say whether a PR is queued, nor whether classic branch protection requires a queue (required_checks()
    # reports only a ruleset's): a cloud session, where only REST answers, takes the project's word in loop.md
    if pull["isInMergeQueue"] is None and not (
        in_cloud() and settings(project()[1]).get("land", {}).get("merge_queue") is False
    ):
        return (
            f"{where}: GitHub's REST API doesn't say whether it is in a merge queue, which would merge it with GitHub's"
            " signature: land it where GraphQL answers, or, where the base requires no merge queue, say so with"
            " `merge_queue: false` in loop.md's `land` section"
        )
    # the URL as configured, before any `insteadOf` rewrite
    origin = subprocess.run(
        ["git", "config", "--get", "remote.origin.url"], capture_output=True, text=True, check=False
    )
    if github_repo(origin.stdout.strip()) != pull["baseRepository"]["nameWithOwner"].lower():
        return f"this checkout's `origin` is not {pull['baseRepository']['nameWithOwner']}, where {where} lives"
    # with SIGNING, as the land signs: a cloud land's own key over the VM's
    config = subprocess.run(
        ["git", *SIGNING, "config", "--get", "user.signingkey"],
        capture_output=True,
        text=True,
        check=False,
    )
    gpg = subprocess.run(
        ["git", *SIGNING, "config", "--get", "gpg.format"],
        capture_output=True,
        text=True,
        check=False,
    )
    if not config.stdout.strip() or gpg.stdout.strip() != "ssh":
        return "this checkout signs no commits with an SSH key: set `gpg.format ssh` and `user.signingkey`"
    if not signers.is_file():
        return f"no allowed signers file at {signers}"
    return None


def signed_squash(pull: dict[str, Any], signers: Path, onto: tuple[str, str] | None = None) -> str:
    """Squash the PR's head onto its base here, signed with this checkout's key and checked against `signers`, then
    push it to the PR's branch and fast-forward the base to it: GitHub marks a PR merged once its head is in the
    base. The head must be the one the proofs judged and contain the base's tip, so the squash undoes nothing; with
    `onto` (a commit and a tree, from land's batch), the base's tip must be that commit and the squash holds that
    tree, the head merged into the batch."""
    if not shutil.which("ssh-keygen"):
        raise Refused(NO_KEYGEN)
    pr, base, branch, head = (
        pull["number"],
        pull["baseRefName"],
        pull["headRefName"],
        pull["headRefOid"],
    )
    git("fetch", "-q", "origin", f"+refs/heads/{base}:refs/remotes/origin/{base}")
    git(
        "fetch", "-q", "origin", f"refs/pull/{pr}/head"
    )  # alone: FETCH_HEAD names the first ref fetched
    if git("rev-parse", "FETCH_HEAD").strip() != head:
        raise Refused(f"PR #{pr}'s head moved past {head}: run land again")
    tip = git("rev-parse", f"refs/remotes/origin/{base}").strip()
    if onto is not None:
        if tip != onto[0]:
            raise Refused(
                f"PR #{pr}: {base} moved to {tip} since the batch was merged onto {onto[0]}: land again"
            )
        tree = onto[1]
    elif subprocess.run(["git", "merge-base", "--is-ancestor", tip, head], check=False).returncode:
        raise Refused(
            f"PR #{pr} does not contain {base}'s head {tip}: merge origin/{base} in, rerun the gates, get a"
            " verdict on the merge, then land again"
        )
    else:
        tree = git("rev-parse", f"{head}^{{tree}}").strip()
    try:
        commit = git("commit-tree", "-S", "-p", tip, "-m", f"{pull['title']} (#{pr})", tree).strip()
        git("-c", f"gpg.ssh.allowedSignersFile={signers}", "verify-commit", commit)
    except subprocess.CalledProcessError as exc:
        raise Refused(
            f"PR #{pr}: the squash is not signed by a key in {signers}: {' '.join(exc.stderr.split())}"
        ) from exc
    # the PR's branch first, pinned to the head the gates checked: GitHub marks the PR merged only when its head is
    # already the PR's branch as the base reaches it (one atomic push of both leaves the PR closed instead)
    git(
        "push",
        "-q",
        f"--force-with-lease=refs/heads/{branch}:{head}",
        "origin",
        f"{commit}:refs/heads/{branch}",
    )
    try:
        git(
            "push",
            "-q",
            f"--force-with-lease=refs/heads/{base}:{tip}",
            "origin",
            f"{commit}:refs/heads/{base}",
        )
    except subprocess.CalledProcessError as exc:
        # a base that moved meanwhile, or refused the push: put the PR's branch back on the checked head, leased to
        # the squash so a push to the branch since then stays
        git(
            "push",
            "-q",
            f"--force-with-lease=refs/heads/{branch}:{commit}",
            "origin",
            f"{head}:refs/heads/{branch}",
        )
        raise Refused(
            f"PR #{pr}: {base} refused the squash, so {branch} is back at {head}: {' '.join(exc.stderr.split())}"
        ) from exc
    return commit


def outside(pull: dict[str, Any], loop: str | None) -> str | None:
    """Why the PR is outside the loop, or None when it left a loop trail or gatekeeper.md lacks `opt_out: true`. A trail
    is a verifier review (passes), an `Approved: spec` record or `approved:spec` on an issue it links, a `Reverts #<n>`
    line, or a bot PR gatekeeper.md exempts. A spec a policy covers is no trail: under `spec.auto: true` every issue is covered,
    so a person who opted out could never link one."""
    if not opted_out(loop):
        return None
    found = passes(pull)
    if (isinstance(found, str) or found) or exempt(pull, loop):
        return None
    if REVERTS.search(QUOTED.sub("", pull["body"] or "")):
        return None
    for issue in closing_issues(pull):
        if READY in names(issue) or records(issue, "spec"):
            return None
    return (
        f"PR #{pull['number']} is outside the loop ({source(loop)} `opt_out: true`, no verifier review, spec approval,"
        " revert or bot exemption): its author merges it"
    )


def ran(pull: dict[str, Any]) -> list[str]:
    """One line per check the merge waits for on the head: ci()'s lines for the checks the base branch requires but the
    loop's own statuses (OWN), since GitHub merges nothing without them. A base that requires none waits for nothing:
    no aggregate `check`, and under `ci: false` no local-ci pass, since the verifier's record is the merge's proof and
    no gate runs between it and the merge (#1872)."""
    # the approvals status is check merge's own output: waiting for it never ends
    required = required_checks(pull["baseRefName"]) - OWN
    return ci(f"PR #{pull['number']}", pull, required) if required else []


def merge_base_of(base: str, head: str) -> str | None:
    """`git merge-base origin/<base> <head>`, origin's `base` fetched first, else None where git can't answer."""
    with contextlib.suppress(subprocess.CalledProcessError, OSError):
        git("fetch", "-q", "--no-tags", "origin", f"+refs/heads/{base}:refs/remotes/origin/{base}")
    with contextlib.suppress(subprocess.CalledProcessError, OSError):
        return git("merge-base", f"origin/{base}", head).strip()
    return None


def trusted(pull: dict[str, Any], note: dict[str, Any], author: bool = True) -> bool:
    """Whether a proof comment's author counts: the account running this, a writer (writer), and with `author` the
    PR's author, as passes() reads verifier reviews, since anyone can comment on a public repo."""
    login = (note.get("author") or {}).get("login")
    if login is None:
        return False
    if author and login == (pull.get("author") or {}).get("login"):
        return True
    return login == viewer() or writer(login)


def merged(
    first: str, second: str, guard: list[str] | None = None, base: str = "the base"
) -> tuple[str, str] | str:
    """The tree `git merge-tree --write-tree` makes of commits `first` and `second`, so a merge with that tree resolved
    no conflict and added nothing, and their merge base; a string names why not. With `guard`, the globs of gatekeeper.md's
    `carry_over_main.except`, the proofs on `first` carry over to that merge only when `second`'s changes since the
    merge base (`base`'s) touch no file `first`'s changes touch, neither side's changes touch a file a glob matches
    (fnmatch on the full path, as a rule's `paths`), and the two have one merge base. `first` is the PR's head, so a
    why names its side as the PR's. Fail-closed: a git error is a why, never a carry."""
    try:
        clean = subprocess.run(
            ["git", "merge-tree", "--write-tree", first, second],
            capture_output=True,
            text=True,
            check=False,
        )
        if clean.returncode or not clean.stdout.split():
            return f"{first} and {second} conflict"
        tree = clean.stdout.split()[0]
        if guard is None:
            return tree, ""
        bases = git("merge-base", "--all", first, second).split()
        if len(bases) != 1:
            return f"{first} and {second} have {len(bases)} merge bases"
        # plumbing: porcelain `git diff` follows config (diff.relative, a submodule's `ignore = all`) that hides paths
        changed = [
            set(
                filter(
                    None,
                    git(
                        "diff-tree",
                        "-r",
                        "--name-only",
                        "--no-renames",
                        "--ignore-submodules=none",
                        "-z",
                        bases[0],
                        side,
                    ).split("\0"),
                )
            )
            for side in (first, second)
        ]
        if both := sorted(changed[0] & changed[1]):
            return f"{both[0]} changed on both sides since {bases[0]}"
        # the PR's own config change was gated only on the PR's code, never on what the base added since
        for side, paths in ((f"{base}'s", changed[1]), ("the PR's", changed[0])):
            if hit := sorted(
                path for path in paths if any(fnmatch.fnmatch(path, glob) for glob in guard)
            ):
                return f"{side} changes since {bases[0]} touch {hit[0]}, a `carry_over_main.except` path"
        return tree, bases[0]
    except (subprocess.CalledProcessError, OSError) as exc:
        return f"git failed reading the merge of {first} and {second}: {exc}"


def carry_line(what: str, head: str, second: str, since: str, base: str) -> str:
    """The line check merge and land print when `what` carries the proofs of `head` (carried, merged)."""
    return (
        f"{what} carries the proofs of {head}: it is the clean merge (git merge-tree) of {head} and {base}'s"
        f" {second}, whose changes since {since} touch none of the PR's files, and neither side touches a"
        " `carry_over_main.except` path"
    )


def carried(
    pull: dict[str, Any], loop: str | None, verified: str
) -> tuple[str, list[str]] | str | None:
    """`verified` with one carry_line() per merge, oldest first, when the PR's head carries the verdict on
    `verified`, the newest verdict's commit, where gatekeeper.md has `carry_over_main`: from the head, each commit on its
    first-parent line down to `verified` is a merge whose second parent is on the base (an ancestor of its tip), whose
    tree is git merge-tree's of its parents, and where merged() finds the base's changes disjoint from the PR's and
    neither side's touching an except glob. So a PR commit after `verified` (new, edited or rebased) carries nothing.
    A head with `verified`'s tree (same_tree) carries it with or without the setting, by one same_tree_line(). A
    string names why it carries nothing; None without the setting and with another tree."""
    if tree := same_tree(pull["headRefOid"], verified):
        return verified, [same_tree_line(verified, tree)]
    guard = settings(loop).get("carry_over_main")
    if (
        guard is None
    ):  # a project's own `except` globs (deprecated) keep deciding where it sets them
        proof = catch_up(pull, verified)
        return (
            (verified, [f"verdict on {verified} carried to {pull['headRefOid']}: {proof}"])
            if proof
            else None
        )
    walked = base_merges(pull, verified)
    if isinstance(walked, str):
        return walked
    base, lines = pull["baseRefName"], []
    for commit, first, second in walked:
        found = merged(first, second, guard["except"], base)
        if isinstance(found, str):
            return found
        try:
            tree = git("rev-parse", f"{commit}^{{tree}}").strip()
        except (subprocess.CalledProcessError, OSError) as exc:
            return f"git failed reading {commit}: {exc}"
        if found[0] != tree:
            return f"{commit}'s tree is not git merge-tree's of its parents: the merge resolved or added something"
        lines.insert(0, carry_line(commit, first, second, found[1], base))
    return verified, lines


def same_tree(head: str, old: str) -> str | None:
    """The tree `head` and `old` both hold (tree_of), else None: a head whose tree is an earlier head's keeps that
    head's verdict (carried) and go (carried_go), since every file reviewed and approved there is byte-for-byte the
    same, as after a reword of commit messages only. A tree neither the clone nor origin has is None."""
    mine = tree_of(head)
    return mine if mine is not None and mine == tree_of(old) else None


def catch_up(pull: dict[str, Any], old: str) -> str | None:
    """What shows, mechanically, that the PR's head only takes the base in since `old` (an earlier head), with no hand
    edit, or None: the proof's words, which carried() and whose() print. Either (a rebase) `git range-diff` of
    `merge-base(old, tip)..old` against `merge-base(head, tip)..head` marks every commit `=` with none added or dropped
    and neither range holds a merge commit, which range-diff does not show (#1497),
    or (a merge of the base) every commit since `old` is a merge of the base (base_merges) and `git show --remerge-diff`
    of each is empty, so no conflict was resolved by hand. A verdict (carried) and a go (carried_go) on `old` carry to
    the head by this one test, which nobody asserts. Fail-closed: a git error, or a commit origin lacks, is None (#1314)."""
    head, old, tip = pull["headRefOid"].lower(), old.lower(), pull["baseRefOid"].lower()
    base = pull["baseRefName"]
    if head == old or not all(map(fetched, (head, old, tip))):
        return None
    try:
        walked = base_merges(pull, old)
        if not isinstance(walked, str):
            if all(
                not git("show", "--remerge-diff", "--format=", "--no-color", commit).strip()
                for commit, _, _ in walked
            ):
                return f"every commit since is a merge of {base} whose `git show --remerge-diff` is empty: no conflict was resolved by hand"
            return None
        first, second = (git("merge-base", commit, tip).strip() for commit in (old, head))
        # range-diff leaves merge commits out, so a merge whose tree adds an edit would still show every commit `=`
        if any(
            git("rev-list", "--merges", span).strip()
            for span in (f"{first}..{old}", f"{second}..{head}")
        ):
            return None
        shown = git("range-diff", "--no-color", f"{first}..{old}", f"{second}..{head}").splitlines()
    except (subprocess.CalledProcessError, OSError):
        return None
    # one line per commit pair (`1:  abc1234 = 1:  def5678 subject`, padded with a leading space once a side has 10+
    # commits), a changed one followed by its 4-space-indented diff; any other line is not understood: not clean
    pair = re.compile(r"^\s*(?:\d+|-):\s+\S+\s+([=!<>])\s+(?:\d+|-):")
    marks: list[str] = []
    for line in shown:
        if found := pair.match(line):
            marks.append(found[1])
        elif line.strip() and not line.startswith("    "):
            return None
    if marks and set(marks) == {"="}:
        return f"`git range-diff` against {base} shows every commit unchanged: a rebase with no added or dropped commit"
    return None


def same_tree_line(old: str, tree: str) -> str:
    """The line check merge and land print when the head carries `old`'s verdict on the same tree (same_tree)."""
    return f"verdict on {old} carried: same tree {tree}"


def base_merges(pull: dict[str, Any], since: str) -> list[tuple[str, str, str]] | str:
    """Each commit on the PR head's first-parent line down to `since`, newest first, as (commit, first parent, second
    parent), when every one is a merge whose second parent is on the base (an ancestor of its tip), so nothing but
    merges of the base, conflict fixes and all, came after `since`; a string names the first commit that is anything
    else. Fail-closed: a git error is a why."""
    head, tip, base = pull["headRefOid"].lower(), pull["baseRefOid"].lower(), pull["baseRefName"]
    commit, found = head, []
    try:
        if not (fetched(head) and fetched(tip)):
            return f"the head or {base}'s head {tip} is not a commit origin has"
        while commit != since:
            it, its = ("it", "its") if commit == head else (commit, f"{commit}'s")
            parents = git("rev-list", "--parents", "-n", "1", commit).split()[1:]
            if len(parents) != 2:
                return f"{it} is no merge of two parents"
            first, second = parents
            on_base = ["git", "merge-base", "--is-ancestor", second, tip]
            if subprocess.run(on_base, capture_output=True, check=False).returncode:
                return f"{its} second parent {second} is not on {base}"
            found.append((commit, first, second))
            commit = first
    except (subprocess.CalledProcessError, OSError) as exc:
        return f"git failed reading {commit}: {exc}"
    return found


def unforced(pull: dict[str, Any]) -> str | None:
    """The line check merge and brief print when every commit since the newest verdict's commit is a merge of the
    base (base_merges) whose parents merge cleanly (merged) and land posted no CONFLICTS comment naming a head that
    walk passes, so nothing forced the merge: land merges each head onto the base itself and gates that, and the
    merge only costs a gate and a verifier pass. None otherwise, a head or base tip that one fetch (fetched, in
    base_merges) cannot bring included, since the line is advice and blocks nothing."""
    verdicts = passes(pull)
    if isinstance(verdicts, str) or not verdicts or not (verified := verdicts[-1][0]):
        return None
    if verified == pull["headRefOid"].lower():
        return None
    walked = base_merges(pull, verified)
    # a merge whose parents conflict was forced, CONFLICTS comment or not
    if isinstance(walked, str) or any(
        isinstance(merged(first, second), str) for _, first, second in walked
    ):
        return None
    earlier = {first for _, first, _ in walked}
    try:
        found = notes(pull)
    except Refused:
        return None
    # as land writes it (merged_onto), by an account whose word counts, since anyone can comment on a public repo
    if any(
        note["body"].startswith(CONFLICTS)
        and trusted(pull, note)
        and earlier
        & {
            line[len("Head: ") :].strip().lower()
            for line in note["body"].splitlines()
            if line.startswith("Head: ")
        }
        for note in found
    ):
        return None
    base = pull["baseRefName"]
    return f"unforced merge of {base}: land merges heads itself (the lead role, step 8); this pass is avoidable"


def carries(where: str, pull: dict[str, Any], loop: str | None, verified: str) -> bool:
    """Whether the PR's head carries the proofs of `verified`, the newest verdict's commit (carried), printing the
    lines that say so, or where gatekeeper.md opts in, why it carries nothing."""
    carry = carried(pull, loop, verified)
    if carry is None:
        return False
    if isinstance(carry, str):
        print(f"{where}: {pull['headRefOid']} carries nothing from {verified}: {carry}")
        return False
    for line in carry[1]:
        print(f"{where}: {line}")
    return True


def fetched(sha: str) -> bool:
    """Whether `sha` names a commit here, fetching `origin` once when it doesn't yet."""
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
        return False
    have = ["git", "cat-file", "-e", f"{sha}^{{commit}}"]
    if subprocess.run(have, capture_output=True, check=False).returncode == 0:
        return True
    subprocess.run(["git", "fetch", "--quiet", "origin"], capture_output=True, check=False)
    return subprocess.run(have, capture_output=True, check=False).returncode == 0


def owner_waits(
    pull: dict[str, Any],
    issues: list[dict[str, Any]],
    loop: str | None,
    pushed: Callable[[dict[str, Any]], str | None] | None = None,
) -> list[str]:
    """A line while the change's rating asks a go of its merge (rating: `go` or `owner`) and no go is on the head
    (owned): the owner's, or with `loop.delegate: all` the delegate's. Into the default branch only such a head-pinned
    go counts, since a delegate acting through the owner's account adds a label no differently from the owner; into a
    feature branch the `approved:merge` label, added after the head's push, counts too, the owner's or the delegate's
    (delegate). `pushed(pull)` is when the head was pushed (default: GitHub's repository activity). A rating that asks
    `owner`, by default a path `risk.critical_paths` names, takes the owner's own go on the head, into any branch.
    """
    where, found = f"PR #{pull['number']}", []
    rated = rating(pull, issues, loop)
    if rated.merge == "gates":
        return found
    why = rated.because
    carry, sole = carried_go(pull, loop), why if rated.merge == "owner" else None
    by = carry[0] if isinstance(carry, tuple) else None
    if by == "owner" or by and not sole:
        return found
    command = f"`approve merge {pull['number']} --head {pull['headRefOid']} --by owner`"
    if sole:
        # neither the delegate's go nor the label, into any branch: the path is the owner's alone
        return [
            (
                f"{where} waits for the owner's go on its head ({command}; the delegate's go and `{MERGE_LABEL}` are"
                f" no go), since {sole}"
            )
        ]
    # why the go on an earlier head stops short of this one, so the lead knows what a new verdict could fix
    stale = [f"{where}: {carry}"] if isinstance(carry, str) else []
    if default(pull):
        if delegation(base_policy(pull["baseRefName"], loop)) == "all":
            ask = f"{command}, or the delegate's: `approve merge {pull['number']} --head {pull['headRefOid']} --by delegate`"
            more = ""
        else:
            ask, more = (
                command,
                "; `loop.delegate: all` in the policy file lets a delegate's go count",
            )
        return [
            (
                f"{where} waits for the owner's go on its head ({ask}; into {pull['baseRefName']} `{MERGE_LABEL}`"
                f" is no go{more}), since it is {rated.source} risk: {why}"
            ),
            *stale,
        ]
    if MERGE_LABEL not in names(pull):
        return [
            (
                f"{where} waits for the owner's go on its head ({command}, or `{MERGE_LABEL}`), since it is"
                f" {rated.source} risk: {why}"
            ),
            *stale,
        ]
    added = max(
        (
            event["createdAt"]
            for event in pull["timelineItems"]["nodes"]
            if (event.get("label") or {}).get("name") == MERGE_LABEL
        ),
        default="",
    )
    when = (pushed or head_pushed)(pull)
    if when is None:
        found.append(
            f"{where}: GitHub lists no push of the head {pull['headRefOid']}, so `{MERGE_LABEL}` can't be dated after it"
        )
    elif added <= when:  # ISO 8601 in UTC, so it orders as text
        found.append(
            f"{where}: `{MERGE_LABEL}` was added before the head was pushed: the owner adds it again"
        )
    return found


def owned(pull: dict[str, Any], loop: str | None = None) -> str | None:
    """Whose go covers the PR's head, `owner` or `delegate`, else None: one on the head, or one carried to it
    (carried_go)."""
    found = carried_go(pull, loop)
    return found[0] if isinstance(found, tuple) else None


def carried_go(pull: dict[str, Any], loop: str | None) -> tuple[str, str] | str | None:
    """(whose, the head it is on) for the go (go_on) covering the PR's head: the go on the head itself, else the go
    on the head of the PR's newest merge record, when the head has that head's tree (same_tree), or every commit since
    is a merge of the base (base_merges) and the newest verifier pass on the head is satisfied with no blocker or major
    open and says `Logic change: none`, since the approver saw that change and a merge adds none to it. Any status on
    the head decides alone, so a go withdrawn there stays withdrawn. A string names why the older go carries nothing;
    None: no go to carry."""
    head = pull["headRefOid"]
    # a project with its own rule keeps it for the go too
    guard = settings(loop).get("carry_over_main")
    by = go_on(pull, head, loop)
    if by is not None:
        return (by, head) if by else None
    earlier = [
        record["head"]
        for record in records(pull, "merge")
        if record.get("head") and record["head"].lower() != head.lower()
    ]
    if not earlier or not (by := go_on(pull, earlier[-1], loop)):
        return None
    old = earlier[-1]
    if same_tree(head, old) or (guard is None and catch_up(pull, old)):
        return by, old
    why = unchanged(pull, old.lower())
    return f"the go on {old} carries nothing to {head}: {why}" if why else (by, old)


def unchanged(pull: dict[str, Any], old: str) -> str | None:
    """Why the PR's head may not keep the go on `old` (carried_go), or None: the newest verifier pass on the head is
    satisfied with no blocker or major open and says `Logic change: none`, and every commit since `old` is a merge of
    the base (base_merges). The verdict is read first, since it needs no git."""
    head = pull["headRefOid"].lower()
    verdicts = passes(pull)
    if isinstance(verdicts, str):
        return f"the PR {verdicts}"
    on_head = [(n, lines) for commit, n, lines in verdicts if commit == head]
    if not on_head:
        return f"no verifier review is on {head}"
    last = max(n for n, _ in on_head)
    same = [lines for n, lines in on_head if n == last]
    if not all(held(lines) for lines in same):
        return (
            f"the verifier's pass {last} on {head} is not satisfied, or has a blocker or major open"
        )
    if any(lines.get(LOGIC, "").lower().split()[:1] != ["none"] for lines in same):
        return f"the verifier's pass {last} on {head} does not say `{LOGIC}: none`"
    walked = base_merges(pull, old)
    return walked if isinstance(walked, str) else None


def go_on(pull: dict[str, Any], head: str, loop: str | None) -> str | None:
    """Whose go (go) is on `head`, `owner` or `delegate`; '' when a status there is no go; None with no status there:
    the newest `owner` status there by the account running this or a writer (a status is the repo's to write) is green,
    so a later one withdrawing it wins, and the PR holds the record by the same approver naming that head. A record
    recorded_go counts, written after that status, decides over it; with no status, those records decide alone. The
    delegate's (its description says so) counts into the default branch only while the default branch's `delegate` is `all`, so
    removing the setting withdraws it. A status once turned from the label (LABELLED) is no go. GitHub unreachable: no
    go."""
    try:
        found = statuses(head)
    except (subprocess.CalledProcessError, OSError, ValueError):
        return ""
    for status in found:  # newest first
        login = (status.get("creator") or {}).get("login")
        if (
            status.get("context") != OWNER_GO
            or login is None
            or not (login == viewer() or writer(login))
        ):
            continue
        # a record written after this status, where a later status write was refused (a cloud withdrawal or go,
        # #1816), decides over it, so a withdrawal that could only be recorded still takes the go back
        if (at := status.get("created_at")) and (
            later := recorded_go(pull, head, loop, at)
        ) is not None:
            return later
        description = status.get("description") or ""
        by = DELEGATE if description.endswith(DELEGATED) else "owner"
        if (
            status.get("state") != "success"
            or description.endswith(LABELLED)
            or by == DELEGATE
            and default(pull)
            and delegation(base_policy(pull["baseRefName"], loop)) != "all"
        ):
            return ""
        return by if current(pull, "merge", "head", head, by) else ""
    return recorded_go(pull, head, loop)


# the first line of the record `approve merge --withdraw` writes, beside `Approved: merge`
WITHDRAWN = "Withdrawn: merge"
# the line an `Approved: merge` record carries when GitHub refused its `owner` status (unwritten), which lets it stand
# as the go alone (recorded_go); a record without it still needs its status
UNSTATUSED = "Status: refused"


def recorded_go(
    pull: dict[str, Any], head: str, loop: str | None, after: str | None = None
) -> str | None:
    """go_on for a head with no `owner` status, as a cloud session leaves it when its proxy refuses the status write
    (#1816): the newest `Approved: merge` record that says its status was refused (UNSTATUSED), or `Withdrawn: merge`
    record, naming `head` and written by the account running this or a writer (a comment is anyone's to write in a
    public repo, so its author is what counts), decides: whose go it is, '' for a withdrawal or a delegate's go the
    delegation no longer covers, None with no such record. An `Approved: merge` record without the line, as a host
    writes beside its status, is still no go alone. With `after` (an ISO 8601 UTC time, a status's), only records
    written after it count."""
    for note in reversed(notes(pull)):
        if after is not None and not (note.get("createdAt") or "") > after:
            continue
        lines = machine(note["body"]).strip().splitlines()
        if lines and lines[0].strip() in VOICED.values():
            lines = lines[1:]
        if not lines or lines[0].strip() not in {"Approved: merge", WITHDRAWN}:
            continue
        found = fields("\n".join(lines))
        login = (note.get("author") or {}).get("login")
        if (
            found.get("head", "").lower() != head.lower()
            or found.get("by") not in VOICED
            or lines[0].strip() != WITHDRAWN
            and found.get("status") != UNSTATUSED.split(": ", 1)[1].lower()
            or login is None
            or not (login == viewer() or writer(login))
        ):
            continue
        by = found["by"]
        if (
            lines[0].strip() == WITHDRAWN
            or by == DELEGATE
            and default(pull)
            and delegation(base_policy(pull["baseRefName"], loop)) != "all"
        ):
            return ""
        return by
    return None


def whose(pull: dict[str, Any], loop: str | None) -> list[str]:
    """The line check merge and land print for a head that carries a go (owned): whose it is, and the head it was given
    on when carried there (carried_go)."""
    found = carried_go(pull, loop)
    if not isinstance(found, tuple):
        return []
    by, old = found
    head = f"PR #{pull['number']}: the go on {old}"
    line = f"{head} is the delegate's{DELEGATED}" if by == DELEGATE else f"{head} is the owner's"
    if old != pull["headRefOid"] and (tree := same_tree(pull["headRefOid"], old)):
        line += f", carried to {pull['headRefOid']}: same tree {tree}"
    elif (
        old != pull["headRefOid"]
        and "carry_over_main" not in settings(loop)
        and (proof := catch_up(pull, old))
    ):
        line += f", carried to {pull['headRefOid']}: {proof}"
    elif old != pull["headRefOid"]:
        line += (
            f", carried to {pull['headRefOid']}: every commit since is a merge of {pull['baseRefName']}, and the"
            f" verifier's pass on it says `{LOGIC}: none`"
        )
    return [line]


# what a cloud session's GitHub proxy answers a commit-status write it refuses (#1816): comments, labels and the
# merge pass, `POST statuses/<sha>` alone does not
STATUS_REFUSED = "HTTP 403"


def status_refused(exc: BaseException) -> bool:
    """Whether a status write failed because GitHub, or a proxy in front of it, refuses this token the path: a 403.
    Any other failure (unreachable, a 422 on a bad sha) is no refusal, and stops as before."""
    return isinstance(exc, subprocess.CalledProcessError) and STATUS_REFUSED in (exc.stderr or "")


def unwritten(head: str, context: str, description: str, state: str = "success") -> str | None:
    """post_status, and None; the refusal's text when the write is refused (status_refused), printed with what still
    carries the step, so a cloud session goes on: the go's record (go_on reads it with no status there), or
    `loop:queued` and the hand-over record (mergify_step). Any other failure raises."""
    try:
        post_status(head, context, description, state)
    except subprocess.CalledProcessError as exc:
        if not status_refused(exc):
            raise
        why = stderr_of(exc)
        print(f"the `{context}` status on {head} was not written, GitHub refused it: {why}")
        return why
    return None


def go(number: int, head: str, by: str = "owner", withdraw: bool = False) -> int:
    """approve merge: the go on `head`, the PR's head the approver was shown, on their word: the owner's, or with `by`
    `delegate` the delegated approver's under the owner's delegation; the status OWNER_GO on that commit and a record
    naming it, whose first line says whose go it is; with `withdraw`, a newer failing status and record that take it
    back. The script writes every word, so no agent's description of the change travels with it. A head that moved is
    refused, since the approver saw the one they were asked about. The delegate's go into the default branch is
    refused unless `delegate` is `all` in the policy on `origin/<base>` (base_policy), since without it only the owner's
    go covers that branch and a branch's own edit is not the owner's, and on a PR
    whose rating asks the owner's own go of its merge (owners_only; by default a path `risk.critical_paths` names) into
    any branch, since that merge is the owner's alone."""
    pull = pull_request(number)
    if pull["state"] != "OPEN":
        raise Refused(f"PR #{number} is {pull['state'].lower()}: nothing to approve")
    if pull["headRefOid"].lower() != head.lower():
        raise Refused(
            f"PR #{number}'s head is {pull['headRefOid']}, not {head}: ask about the head it has now"
        )
    head = pull["headRefOid"]
    who = "the delegate" if by == DELEGATE else "the owner"
    if by == DELEGATE and not withdraw:
        with policy_from(pull["baseRefName"]):
            loop = project()[1]
        if sole := owners_only(pull, loop):
            raise Refused(
                f"PR #{number}: the delegate's go is no go here, since {sole}: it waits for the owner's"
            )
        if default(pull) and delegation(base := base_policy(pull["baseRefName"], loop)) != "all":
            raise Refused(
                f"PR #{number} targets {pull['baseRefName']}, the default branch: the delegate's go covers it only"
                f" with `loop.delegate: all` in {pull['baseRefName']}'s {source(base)}, so it waits for the owner's"
            )
    if withdraw:
        # the label off too, since into a feature branch it is a go of its own
        label(number, MERGE_LABEL, add=False)
        unwritten(head, OWNER_GO, f"{who.capitalize()} withdrew their go on {head[:12]}", "failure")
        comment(
            number,
            fold(
                WITHDRAWN,
                f"{who.capitalize()} withdrew their go on {head[:7]}.",
                f"By: {by}\nHead: {head}",
            ),
        )
        print(f"PR #{number}: {who}'s go on {head} is withdrawn")
        return 0
    if by == DELEGATE:
        refused = unwritten(head, OWNER_GO, f"The delegate's go on {head[:12]}{DELEGATED}")
    else:
        refused = unwritten(head, OWNER_GO, f"The owner's go on {head[:12]}")
    comment(
        number,
        fold(
            VOICED[by],
            f"Go to merge {head[:7]}.",
            f"Approved: merge\nBy: {by}\nHead: {head}"
            + (f"\n{UNSTATUSED}" if refused is not None else ""),
        ),
    )
    if refused is not None:
        print(f"PR #{number}: the record alone carries the go on {head}")
    print(f"PR #{number}: {who}'s go is on {head}{DELEGATED if by == DELEGATE else ''}")
    if by != DELEGATE and NEEDS_OWNER in names(pull):
        # the owner's go names this PR, so the hold it waited on is theirs to lift; the delegate's never is
        label(number, NEEDS_OWNER, add=False)
        print(f"PR #{number}: `{NEEDS_OWNER}` removed, since the owner's go names it")
    hand_over(number)
    return 0


# The break-glass land (#1722): the owner approves one exact head with a red or missing required check, and land
# verifies the proof from run data and leaves the merge to the owner's own bypass in the GitHub UI. The status context
# and the `Approved:` record's point are one name.
BREAKGLASS = "breakglass"
# the first line of the proof comment (breakglass_proof): run ids and, once verified, how each failed test was classified
PROOF = "Break-glass proof"
# hours an approval lasts when the owner names no `--expires`
BREAKGLASS_HOURS = 48
# the proof's windows when the policy sets none: runs and heads older than 7 days or 20 commits count for nothing, and at
# least 2 earlier green runs show the suite was healthy
BREAKGLASS_AGE, BREAKGLASS_COMMITS, BREAKGLASS_EARLIER = "7d", 20, 2
# the Actions artifact `qualify` publishes (JSON `{"failed": [test ids], "selected": [test ids]}`) that the proof reads
BREAKGLASS_ARTIFACT = "failed-tests"
ISO = "%Y-%m-%dT%H:%M:%SZ"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(text: str) -> datetime | None:
    """An ISO 8601 time as an aware datetime (UTC where it names no zone), None when it is none."""
    try:
        found = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return found if found.tzinfo else found.replace(tzinfo=timezone.utc)


def span(text: str) -> timedelta | None:
    """A duration written as days, hours, minutes and seconds in that order (`7d`, `12h`, `4h30m`, `1d12h`, `90s`) as a
    timedelta, None for anything else or for zero: the one parser of the policy's durations (`breakglass.max_age`,
    `land.wait`, `timeouts`)."""
    if not (found := re.fullmatch(r"(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", text.strip())):
        return None
    days, hours, minutes, seconds = (int(part or 0) for part in found.groups())
    total = timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)
    return total if total else None


def breakglass_settings(loop: str | None) -> dict[str, Any]:
    """The policy's `breakglass` section with its defaults: `max_age` as a timedelta, `max_commits`, `smoke` (the gate
    command, else `check-land`), `earlier_runs`, and the `artifact` name."""
    found = settings(loop)
    given = found.get(BREAKGLASS, {})
    age = str(given.get("max_age", BREAKGLASS_AGE)).strip()
    if (found_span := span(age)) is None:
        raise Refused(
            f"{source(loop)}'s `breakglass.max_age` is {age!r}: write it as `7d` or `12h`"
        )
    return {
        "max_age": found_span,
        "max_commits": given.get("max_commits", BREAKGLASS_COMMITS),
        "smoke": given.get("smoke") or found.get("gate") or "check-land",
        "earlier_runs": given.get("earlier_runs", BREAKGLASS_EARLIER),
        "artifact": given.get("artifact", BREAKGLASS_ARTIFACT),
    }


def fields(body: str) -> dict[str, str]:
    """The lower-cased `Key: value` lines under a comment's first line, as a parser reads it (machine)."""
    return {
        key.strip().lower(): value.strip()
        for key, _, value in (
            line.partition(":") for line in machine(body).strip().splitlines()[1:]
        )
    }


def proofs_on(pull: dict[str, Any], head: str) -> list[dict[str, Any]]:
    """The proof comments (breakglass_proof) that name `head`, oldest first."""
    return [
        note
        for note in notes(pull)
        if (machine(note["body"]).strip().splitlines() or [""])[0].strip() == PROOF
        and fields(note["body"]).get("head", "").lower() == head.lower()
    ]


def proof_hash(note: dict[str, Any]) -> str:
    """The sha256 of a proof comment's text as a parser reads it (machine), which an edit changes."""
    return hashlib.sha256(machine(note["body"]).strip().encode()).hexdigest()


def breakglass_post(number: int, head: str) -> dict[str, Any]:
    """PR #number as the break-glass verbs may act on it: open, at `head`, which is then the full SHA."""
    pull = pull_request(number)
    if pull["state"] != "OPEN":
        raise Refused(f"PR #{number} is {pull['state'].lower()}: nothing to approve")
    if pull["headRefOid"].lower() != head.lower():
        raise Refused(
            f"PR #{number}'s head is {pull['headRefOid']}, not {head}: ask about the head it has now"
        )
    return pull


def breakglass(
    number: int, head: str, reason: str, expires: str | None = None, withdraw: bool = False
) -> int:
    """approve breakglass: the owner's word that this exact head may land with a red or missing required check, once
    land verifies the proof (breakglass_verify): the status BREAKGLASS on the commit and a record naming the head, the
    hash of the newest proof comment on it and when it lapses. Owner only: the caller refuses `--by delegate`, and a
    record counts only when the status and the comment are the owner's login's (standing). `withdraw` posts a newer
    failing status and a `Withdrawn: breakglass` record. A head that moved is refused; a push voids the approval."""
    pull = breakglass_post(number, head)
    head = pull["headRefOid"]
    if withdraw:
        post_status(
            head, BREAKGLASS, f"The owner withdrew their break-glass on {head[:12]}", "failure"
        )
        comment(
            number,
            fold(
                f"Withdrawn: {BREAKGLASS}",
                f"The owner withdrew their break-glass on {head[:7]}.",
                f"By: owner\nHead: {head}",
            ),
        )
        print(f"PR #{number}: the owner's break-glass on {head} is withdrawn")
        return 0
    if not re.fullmatch(r"[0-9a-f]{40}", head.lower()) or head.lower() != head:
        raise Refused(f"PR #{number}: break-glass pins the full 40-character head, got {head}")
    if "\n" in reason or not reason.strip():
        raise Refused("approve breakglass --reason is one line")
    if not (found := proofs_on(pull, head)):
        raise Refused(
            f"PR #{number} has no `{PROOF}` comment on {head}: post one first"
            " (approvals.py breakglass-proof), since the owner's record pins its hash"
        )
    until = parse_time(expires) if expires else utcnow() + timedelta(hours=BREAKGLASS_HOURS)
    if until is None or until <= utcnow():
        raise Refused(f"--expires {expires!r} is no future ISO 8601 time")
    post_status(head, BREAKGLASS, f"The owner's break-glass on {head[:12]}")
    comment(
        number,
        fold(
            VOICED["owner"],
            f"Break-glass for {head[:7]}.",
            f"Approved: {BREAKGLASS}\nBy: owner\nHead: {head}\nProof: {proof_hash(found[-1])}\n"
            f"Expires: {until.astimezone(timezone.utc).strftime(ISO)}\nReason: {reason.strip()}",
        ),
    )
    print(f"PR #{number}: the owner's break-glass is on {head}, until {until.strftime(ISO)}")
    return 0


def breakglass_proof(
    number: int,
    head: str,
    failed: int,
    rerun: int | None,
    smoke: int | None,
    earlier: list[int],
) -> int:
    """breakglass-proof: the proof comment for `head`, which any writer or agent posts. It names run ids, and under
    them what verifying them found (each failed test's classification, with its run links), posted only when every item
    holds. The owner's record pins its hash, and land reads every item from GitHub's runs again, never from these words."""
    pull = breakglass_post(number, head)
    head = pull["headRefOid"]
    lines = [f"Head: {head}", f"Failed-run: {failed}"]
    if rerun is not None:
        lines.append(f"Rerun: {rerun}")
    if smoke is not None:
        lines.append(f"Smoke: {smoke}")
    if earlier:
        lines.append(f"Earlier-runs: {', '.join(map(str, earlier))}")
    heading = f"Run ids for the break-glass on {head[:7]}; land reads each from GitHub's runs."
    # the proof as land will read it, so a refusal is said before the owner is asked to pin it
    with policy_from(pull["baseRefName"]):
        loop = project()[1]
    proved = breakglass_verify(pull, loop, {"body": fold(PROOF, heading, "\n".join(lines))})
    for line in proved.why:
        print(line)
    if proved.why:
        print(f"PR #{number}: no proof posted: fix what is named, or name other runs")
        return 1
    url = comment(
        number,
        fold(
            PROOF,
            heading,
            "\n".join(
                lines + ["", "Found (land reads the runs again, not these lines):", *proved.lines]
            ),
        ),
    )
    print(f"PR #{number}: proof posted for {head}: {url}")
    return 0


class Standing(NamedTuple):
    """What the PR holds of the owner's break-glass on its head: `held`, whether anyone wrote a record or status (else the
    PR is judged as it was before break-glass); `why`, a line per gap, none when the approval is current; the owner's
    `record` and the `proof` comment it pins."""

    held: bool
    why: list[str]
    record: dict[str, str] | None = None
    proof: dict[str, Any] | None = None


def standing(pull: dict[str, Any], loop: str | None) -> Standing:
    """The owner's break-glass on the PR's head (section 1 and the refusals of section 5 that need no run): the newest
    `breakglass` status by the owner's login (loop.owner) is green, the record names the head with `By: owner` in the
    owner's own comment, it has not lapsed, and the proof comment still hashes to what it pins. Any other login's
    status, record or `--by` counts for nothing and is named."""
    head, where = pull["headRefOid"], f"PR #{pull['number']}"
    found = [
        (note, record)
        for note in notes(pull)
        if (record := parsed(note["body"], BREAKGLASS)) is not None
    ]
    try:
        marks = [s for s in statuses(head) if s.get("context") == BREAKGLASS]
    except (subprocess.CalledProcessError, OSError, ValueError):
        return Standing(bool(found), [f"{where}: GitHub's statuses on {head} can't be read"])
    if not found and not marks:
        return Standing(False, [])
    owner = str(settings(loop).get("owner") or "").lower()
    if not owner:
        return Standing(
            True,
            [f"{where}: the policy sets no `loop.owner`, so no login is the owner's break-glass"],
        )
    why = []
    theirs = [s for s in marks if str((s.get("creator") or {}).get("login")).lower() == owner]
    if not theirs:
        by = (marks[0].get("creator") or {}).get("login") if marks else None
        why.append(
            f"{where}: no `{BREAKGLASS}` status on {head} by the owner {owner}"
            + (f" (the one there is {by}'s)" if by else "")
        )
    elif theirs[0].get("state") != "success":
        why.append(f"{where}: the owner's break-glass on {head} is withdrawn")
    on_head = [
        (note, record) for note, record in found if record.get("head", "").lower() == head.lower()
    ]
    owned = [
        (note, record)
        for note, record in on_head
        if record.get("by") == "owner"
        and str((note.get("author") or {}).get("login")).lower() == owner
    ]
    if not owned:
        if on_head:
            by = ", ".join(
                sorted(
                    {
                        f"By: {r.get('by')}, comment by {(n.get('author') or {}).get('login')}"
                        for n, r in on_head
                    }
                )
            )
            why.append(
                f"{where}: the `Approved: {BREAKGLASS}` record on {head} is not the owner's ({by})"
            )
        elif found:
            why.append(
                f"{where}: the owner's break-glass names {found[-1][1].get('head', 'no head')}, not the head {head}:"
                " a push voids it"
            )
        else:
            why.append(f"{where}: no `Approved: {BREAKGLASS}` record names {head}")
        return Standing(True, why)
    record = owned[-1][1]
    until = parse_time(record.get("expires", ""))
    if until is None or until <= utcnow():
        why.append(
            f"{where}: the owner's break-glass on {head} lapsed at {record.get('expires') or 'no time'}"
        )
    pinned = [note for note in proofs_on(pull, head) if proof_hash(note) == record.get("proof")]
    if not pinned:
        edited = bool(proofs_on(pull, head))
        why.append(
            f"{where}: the proof comment on {head} "
            + (
                "was edited after the owner approved it (its hash no longer matches their record)"
                if edited
                else "is missing"
            )
        )
    return Standing(True, why, record, pinned[-1] if pinned else None)


# the events whose run of `qualify` runs the whole check; a `workflow_dispatch` run runs only its `tests` input
FULL_EVENTS = ("pull_request", "push", "schedule")


class Proved(NamedTuple):
    """breakglass_verify's answer: `why`, a line per refusal (none: every item holds); `lines`, what was found, one line per
    fact (the classification of each failed test with its run links, each item's run); `infra`, a failure with no test ids."""

    why: list[str]
    lines: list[str]
    infra: bool = False


def smoke_recorded(head: str, command: str) -> bool:
    """Whether a local-ci record for `head`'s tree holds `command` with exit 0 (the record `local-ci --status` reads)."""
    try:
        held = str(tree_of(head))
        record = json.loads(recorded(held, command).read_text())
    except (OSError, ValueError, subprocess.CalledProcessError):
        return False
    return isinstance(record, dict) and (
        record.get("tree"),
        record.get("command"),
        record.get("exit"),
    ) == (
        held,
        command,
        0,
    )


def breakglass_verify(pull: dict[str, Any], loop: str | None, proof: dict[str, Any]) -> Proved:
    """Section 2: every item of the proof read from GitHub's runs, never from the proof comment's words, which only name
    the run ids. A run counts only in this repo, completed, within `breakglass.max_age` of its `completed_at`, and, but
    for the failed run's own, of the workflow that produces the required check. 2a the failing run on the exact head and
    the test ids it failed (`infra` when it published none because the runner or CI was down); 2b each failed test
    classified against completed runs on `main`'s recent heads and the merge base (`not caused by this change` when it
    failed there too, else `unexplained`, which refuses); 2c the failed tests re-run alone on this head and green
    (not for `infra`); 2d a smoke run: a local-ci record for the head's tree under `breakglass.smoke`, or a green
    full Actions run on the head; 2e at least `breakglass.earlier_runs` green full runs on recent heads that are ancestors
    of the head or of the base. A full run is one that ran the whole required check: of the workflow that produces it,
    started by a `pull_request`, `push` or `schedule` (never a `workflow_dispatch`, whose `tests` input can empty or
    narrow it), with a green job of each required check. A failure with no test ids is `infra` only when a job shows CI
    down (cancelled, timed out, no steps); otherwise it is the change's own and refused."""
    cfg, head, base = breakglass_settings(loop), pull["headRefOid"], pull["baseRefName"]
    where, repo = f"PR #{pull['number']}", pull["baseRepository"]["nameWithOwner"].lower()
    ids, why, lines = fields(proof.get("body", "")), [], []
    now = utcnow()

    def mine(run: dict[str, Any]) -> bool:
        return all(
            str((run.get(key) or {}).get("full_name")).lower() == repo
            for key in ("repository", "head_repository")
        )

    def fresh(run: dict[str, Any]) -> bool:
        when = parse_time(run.get("completed_at") or run.get("updated_at") or "")
        return (
            run.get("status") == "completed" and when is not None and now - when <= cfg["max_age"]
        )

    def read(key: str, role: str) -> dict[str, Any] | None:
        raw = ids.get(key, "")
        if not raw.isdigit():
            why.append(f"{where}: the proof names no {role} run (`{key.capitalize()}:`)")
            return None
        run = run_info(int(raw))
        if not mine(run):
            why.append(f"{where}: run {raw} ({role}) is not this repository's")
        elif not fresh(run):
            why.append(
                f"{where}: run {raw} ({role}) is older than breakglass.max_age or not complete"
            )
        else:
            return run
        return None

    def near(sha: str, onto: str) -> bool:
        """`sha` lies within `max_commits` of the head: the commits either side of it."""
        found = compare(sha, onto)
        return found["ahead_by"] + found["behind_by"] <= cfg["max_commits"]

    def link(run: dict[str, Any]) -> str:
        return run.get("html_url") or f"run {run['id']}"

    def tests(run_id: int) -> dict[str, Any] | None:
        return run_artifact(run_id, cfg["artifact"])

    def full(run: dict[str, Any], flow: Any, required: set[str]) -> bool:
        """The run ran the full selection of the required check: of the workflow that produces it, started by a
        `pull_request`, `push` or `schedule` (never a `workflow_dispatch`, whose `tests` input narrows it to nothing or
        to a few ids), with a green job of each required check."""
        if run.get("workflow_id") != flow or run.get("event") not in FULL_EVENTS:
            return False
        return not required or required <= {
            job["name"] for job in run_jobs(run["id"]) if job.get("conclusion") == "success"
        }

    try:
        failed = read("failed-run", "failed")
        if failed is None:
            return Proved(why, lines)
        flow = failed.get("workflow_id")
        if failed.get("head_sha") != head or failed.get("conclusion") != "failure":
            why.append(f"{where}: run {failed['id']} is not a failure on the head {head}")
        required = required_checks(base) - OWN
        if required and not required & {job["name"] for job in run_jobs(failed["id"])}:
            why.append(
                f"{where}: run {failed['id']} is of a workflow that produces none of the required checks"
                f" ({', '.join(sorted(required))})"
            )
        got = tests(failed["id"])
        broken = {"cancelled", "timed_out", "startup_failure"}
        down = any(
            job.get("conclusion") in broken or not job.get("steps")
            for job in run_jobs(failed["id"])
        )
        if got is None and not down:
            why.append(
                f"{where}: run {failed['id']} published no `{cfg['artifact']}` artifact and no job shows CI down:"
                " the failed tests are unknown"
            )
        want = set(got.get("failed", [])) if got else set()
        infra = not want and down
        if not want and not infra and got is not None:
            why.append(
                f"{where}: run {failed['id']} failed with no failed test ids and no job shows CI down:"
                " a failure that is no test's is the change's own, so it is refused"
            )
        lines.append(
            f"{where}: failing run {link(failed)}: "
            + (
                "infra (no failed test ids), so only the smoke run and earlier green runs apply"
                if infra
                else f"{len(want)} failed test(s)"
            )
        )
        if not infra:
            earlier: dict[str, list[str]] = {}
            tip = merge_base(base, head)
            seen: set[int] = set()
            for run in workflow_runs(flow, branch=base) + workflow_runs(flow, head=tip):
                if run["id"] in seen or run.get("head_sha") == head:
                    continue
                seen.add(run["id"])
                if run.get("conclusion") != "failure" or not mine(run) or not fresh(run):
                    continue
                if not near(run["head_sha"], head):
                    continue
                for test in (tests(run["id"]) or {}).get("failed", []):
                    earlier.setdefault(test, []).append(link(run))
            for test in sorted(want):
                if test in earlier:
                    lines.append(
                        f"`{test}`: not caused by this change; it also failed in {', '.join(earlier[test])}"
                    )
                else:
                    lines.append(
                        f"`{test}`: unexplained; it failed on no recent run of main or the merge base"
                    )
                    why.append(
                        f"{where}: `{test}` is unexplained: only this change can explain it, so it is refused"
                    )
            rerun = read("rerun", "re-run")
            if rerun is not None:
                got_rerun = tests(rerun["id"])
                if (
                    rerun.get("head_sha") != head
                    or rerun.get("conclusion") != "success"
                    or rerun.get("workflow_id") != flow
                ):
                    why.append(
                        f"{where}: run {rerun['id']} is not a green run of the workflow on the head {head}"
                    )
                elif not got_rerun or not want <= set(got_rerun.get("selected", [])):
                    why.append(f"{where}: run {rerun['id']} did not re-run every failed test alone")
                else:
                    lines.append(
                        f"{where}: the failed tests passed alone on the head: {link(rerun)}"
                    )
        smoke = ids.get("smoke")
        if smoke:
            run = read("smoke", "smoke")
            if run is not None and (
                run.get("head_sha") != head
                or run.get("conclusion") != "success"
                or not full(run, flow, required)
            ):
                why.append(
                    f"{where}: run {smoke} is not a green full run of the required check on the head {head}"
                    " (a `workflow_dispatch` run, or one of another workflow, is not one)"
                )
            elif run is not None:
                lines.append(f"{where}: smoke run {link(run)}")
        elif smoke_recorded(head, cfg["smoke"]):
            lines.append(f"{where}: smoke: local-ci `{cfg['smoke']}` passed on the head's tree")
        else:
            why.append(
                f"{where}: no smoke proof on the head: `local-ci` of `{cfg['smoke']}` for its tree, or a green Actions run"
                " (`Smoke:`)"
            )
        good: set[int] = set()
        for raw in ids.get("earlier-runs", "").replace(",", " ").split():
            if not raw.isdigit() or int(raw) in good:
                continue
            run = run_info(int(raw))
            if (
                mine(run)
                and fresh(run)
                and run.get("conclusion") == "success"
                and full(run, flow, required)
                and run.get("head_sha") != head
                and near(run["head_sha"], head)
                and any(
                    compare(run["head_sha"], onto)["status"] in ("ahead", "identical")
                    for onto in (head, base)
                )
            ):
                good.add(run["id"])
                lines.append(f"{where}: earlier green run {link(run)}")
        if len(good) < cfg["earlier_runs"]:
            why.append(
                f"{where}: {len(good)} earlier green run(s) of the required check in the window, not the"
                f" {cfg['earlier_runs']} breakglass.earlier_runs asks"
            )
    except (subprocess.CalledProcessError, OSError, ValueError, KeyError) as exc:
        return Proved(
            [f"{where}: a run the proof names can't be read from GitHub: {stderr_of(exc)}"], lines
        )
    return Proved(why, lines, infra)


class Covered(NamedTuple):
    """breakglass_cover's answer: the lines that still hold the merge, and the lines to say beside them."""

    lines: list[str]
    said: list[str]


# a line ci() writes for a required check that is red, or one the head lacks, which a break-glass may cover; a pending
# one, or a head with more checks than one page, is nothing it covers
CI_RED = re.compile(r": `([^`]+)` is (\w+) on the head commit$")
CI_MISSING = re.compile(r": no `([^`]+)` on the head commit yet")


def breakglass_cover(pull: dict[str, Any], loop: str | None, lines: list[str]) -> Covered:
    """ci()'s lines for the head, as they stand with the owner's break-glass: with a current approval (standing) and
    every line a required check red or missing, none, and a line saying so; with a record that is no approval, the
    lines and what is wrong with it; with no record or status, or a pending check among the lines, the lines as they
    were. Every other proof stays required: this replaces the green gate and nothing else."""
    if not lines or no_ci(loop):
        return Covered(lines, [])
    names: dict[str, str] = {}
    for line in lines:
        if red := CI_RED.search(line):
            if red[2].upper() not in RED:
                return Covered(lines, [])
            names[red[1]] = "red"
        elif missing := CI_MISSING.search(line):
            names[missing[1]] = "missing"
        else:
            return Covered(lines, [])
    stand = standing(pull, loop)
    if not stand.held:
        return Covered(lines, [])
    if stand.why:
        return Covered(lines + stand.why, [])
    proved = breakglass_verify(pull, loop, stand.proof or {})
    if proved.why:
        return Covered(lines + proved.why, [])
    what = ", ".join(f"`{name}` {state}" for name, state in names.items())
    return Covered(
        [],
        [
            f"PR #{pull['number']}: required check {what}; break-glass approved by the owner on {pull['headRefOid']}",
            *proved.lines,
        ],
    )


def changed_lines(patch: str) -> list[tuple[list[str], list[str]]]:
    """Each run of changed lines in a file's patch, in order, as the lines it adds and the lines it removes; a context
    or `@@` line ends a run, and a line that ends without a newline is marked so."""
    runs: list[tuple[list[str], list[str]]] = []
    last: list[str] | None = None
    for line in patch.split("\n"):
        if line.startswith("\\") and last:  # `\ No newline at end of file`, about the line before
            last[-1] += "\n\\ no newline"
        elif line[:1] in ("+", "-"):
            if last is None:
                runs.append(([], []))
            last = runs[-1][0 if line[0] == "+" else 1]
            last.append(line[1:])
        else:
            last = None
    return runs


# the status a file has in the PR that undoes a change of that status
UNDONE = {"added": "removed", "removed": "added", "modified": "modified", "renamed": "renamed"}


def inexact(
    original: list[dict[str, Any]],
    revert: list[dict[str, Any]],
    number: int,
    undone: tuple[dict[str, tuple[str, str]] | None, dict[str, tuple[str, str]] | None],
    undoing: tuple[dict[str, tuple[str, str]] | None, dict[str, tuple[str, str]] | None],
) -> list[str]:
    """Why `revert` does not undo #number, both as GitHub's REST API lists a PR's files; empty when it does. Each file
    must have the inverse status and the inverse runs of changed lines in order, while line numbers and context move
    with main. Every path either touches must have, in `undoing` (the revert's merge base and head trees), the modes
    it had in `undone` (#number's base and merge commit trees) the other way round, absence included. A tree GitHub
    truncated is None. Trees map a path to its (mode, blob): a revert that touches only #number's paths and whose
    trees hold #number's blobs the other way round is exact whatever its diff looks like, since a diff can pair the
    same change's lines another way (#351)."""
    why: list[str] = []
    before, after = undone
    base, head = undoing
    trees = None not in (before, after, base, head)

    def paths(entries: list[dict[str, Any]]) -> set[str]:
        return {
            entry[name]
            for entry in entries
            for name in ("filename", "previous_filename")
            if entry.get(name)
        }

    touched = paths(original) | paths(revert)
    if (
        trees
        and touched == paths(original)
        and all(base.get(p) == after.get(p) and head.get(p) == before.get(p) for p in touched)
    ):
        return why

    def changes(entries: list[dict[str, Any]], inverted: bool) -> dict[tuple[str, str], Any]:
        found = {}
        for entry in entries:
            old, new = entry.get("previous_filename") or entry["filename"], entry["filename"]
            if (
                not entry.get("changes") or "patch" not in entry
            ):  # binary, too large, or only renamed or moded
                why.append(f"GitHub shows no diff of `{new}` to compare")
            runs = changed_lines(entry.get("patch") or "")
            status = entry.get("status", "")
            found[(new, old) if inverted else (old, new)] = (
                (UNDONE.get(status, f"no inverse of {status}"), [(r, a) for a, r in runs])
                if inverted
                else (status, runs)
            )
        return found

    def shown(old: str, new: str) -> str:
        return new if old == new else f"{old} -> {new}"

    expected, actual = changes(original, inverted=True), changes(revert, inverted=False)
    for key in sorted(expected.keys() | actual.keys()):
        if key not in actual:
            why.append(f"it leaves `{shown(key[1], key[0])}` as #{number} changed it")
        elif key not in expected:
            why.append(f"it changes `{shown(*key)}`, which #{number} did not")
        elif actual[key][0] != expected[key][0]:
            why.append(
                f"`{shown(*key)}` is {actual[key][0]}, and undoing #{number} needs {expected[key][0]}"
            )
        elif actual[key][1] != expected[key][1]:
            lines, undo = (
                Counter(
                    (sign, line) for run in runs for sign, side in zip("+-", run) for line in side
                )
                for runs in (actual[key][1], expected[key][1])
            )
            extra, missing = sum((lines - undo).values()), sum((undo - lines).values())
            why.append(
                f"`{shown(*key)}` has {extra} line(s) beyond the inverse of #{number}, {missing} missing"
                if extra or missing
                else f"`{shown(*key)}` changes the lines #{number} changed in another order"
            )
    if not trees:
        why.append("GitHub truncates a tree it would compare file modes in")
    else:
        for path in sorted(touched):
            for theirs, ours, where, when in (
                (after, base, "at the merge base", "after"),
                (before, head, "at the head", "before"),
            ):
                mode, wanted = (ours.get(path, ("absent",))[0], theirs.get(path, ("absent",))[0])
                if mode != wanted:
                    why.append(f"`{path}` has mode {mode} {where}, and {wanted} {when} #{number}")
    return why


def reverts(pull: dict[str, Any]) -> tuple[int | None, list[str]]:
    """The merged PR this one exactly reverts, or a line saying why its `Reverts #<n>` is no exact revert; (None, [])
    for a PR without one."""
    found = REVERTS.search(QUOTED.sub("", pull["body"] or ""))
    if not found:
        return None, []
    repo, number = found.group(1), int(found.group(2))
    base = pull["baseRepository"]["nameWithOwner"]
    if repo and repo.lower() != base.lower():
        why = [f"it names a PR of {repo}, and this is {base}"]
    elif not (original := merged_pull(number)):
        why = [f"#{number} is not a merged pull request"]
    else:
        changes, theirs = pr_changes(number), pr_changes(pull["number"])
        why = [
            f"GitHub lists {len(listed)} of {where}'s {total} files"
            for where, listed, total in (
                (f"#{number}", changes, original["changed_files"]),
                (f"PR #{pull['number']}", theirs, pull["files"].get("totalCount") or len(theirs)),
            )
            if len(listed) < total
        ] or inexact(
            changes,
            theirs,
            number,
            (tree(original["base"]["sha"]), tree(original["merge_commit_sha"])),
            (tree(merge_base(pull["baseRefOid"], pull["headRefOid"])), tree(pull["headRefOid"])),
        )
    if not why:
        return number, []
    return None, [
        f"PR #{pull['number']} is not an exact revert of #{number}: {'; '.join(why)}. It needs every proof of any PR"
    ]


def head_checks(
    pull: dict[str, Any], required: set[str]
) -> tuple[list[tuple[str, str]], int | None, set[str]]:
    """The state of the newest run of each check on the head the merge reads, as (name, state): `required` (empty:
    every check); how many checks GitHub returned when it holds more than that one page, else None; and the names
    whose only run is a draft's skip, which the ready PR's CI has not reached yet."""
    commits = pull["commits"]["nodes"]
    contexts = commits[0]["commit"]["statusCheckRollup"] if commits else None
    every = contexts["contexts"]["nodes"] if contexts else []
    more = (
        len(every)
        if contexts and (contexts["contexts"].get("totalCount") or 0) > len(every)
        else None
    )
    wanted = required
    # CI skips a draft, and GitHub counts the skipped run as passing: one completed before the newest ready-for-review
    # is the draft's and says nothing about the ready PR. A run that completed on the draft is the head's result.
    ready = max(
        (
            event["createdAt"]
            for event in pull["timelineItems"]["nodes"]
            if event.get("__typename") == "ReadyForReviewEvent"
        ),
        default="",
    )
    # A rerun, or a run cancelled by a newer one in its concurrency group, leaves several runs of one check on the
    # head; only the newest says whether it is green. One job name in two workflows is two checks.
    newest: dict[tuple[str, str], dict[str, Any]] = {}
    drafts: set[tuple[str, str]] = set()

    def skipped(node: dict[str, Any]) -> bool:
        return node.get("conclusion") == "SKIPPED" and (node.get("completedAt") or "") < ready

    # A workflow whose run since ready has finished never produces a name only its draft run had, such as a matrix
    # job's literal `matrix.part`, skipped before the matrix expanded. Only a finished run says so: a job that needs
    # another has no check run until it starts. A draft's skip is no such run, though GitHub may give it a
    # `startedAt` after its `completedAt`, and so after a ready right behind it.
    finished = {
        workflow(node)
        for node in every
        if not skipped(node)
        and started(node) >= ready
        and (node.get("checkSuite") or {}).get("status") == "COMPLETED"
    }
    for node in every:
        name = node.get("name") or node.get("context")
        if wanted and name not in wanted:
            continue
        if skipped(node):
            drafts.add((workflow(node), name))
            continue
        key = (workflow(node), name)
        if key not in newest or started(node) >= started(newest[key]):
            newest[key] = node
    checks = [
        (name, node.get("conclusion") or node.get("state") or node.get("status"))
        for (_, name), node in newest.items()
    ]
    # a draft skip is replaced only by its own workflow's run: a job of that name in another workflow says nothing
    return checks, more, {name for flow, name in drafts - newest.keys() if flow not in finished}


def ci(where: str, pull: dict[str, Any], required: set[str]) -> list[str]:
    """A line per check head_checks() reads that is not green on the head, or not there yet."""
    checks, more, drafts = head_checks(pull, required)
    wanted = required
    if wanted:
        missing = [f"`{name}`" for name in sorted(wanted - {name for name, _ in checks})]
    else:  # every check of none would pass a head CI has not reached yet, or one only the draft skipped
        missing = [f"`{name}`" for name in sorted(drafts)] or ([] if checks else ["check"])
    if more is not None and (missing or not wanted):
        return [f"{where} has more than {more} {PAGED}"]
    # ponytail: a job that skips for another reason in a workflow without `ready_for_review` never reruns, so it waits
    # for good; the line names the cause. Bound the wait by time since ready if a project hits it.
    late = " since the PR went ready (its workflow may lack the `ready_for_review` type)"
    return [
        f"{where}: no {name} on the head commit yet{late if name.strip('`') in drafts else ''}"
        for name in missing
    ] + [
        f"{where}: `{name}` is {state} on the head commit"
        for name, state in checks
        if state not in GREEN
    ]


def workflow(node: dict[str, Any]) -> str:
    """The name of the workflow a check run belongs to; empty for a status."""
    run = (node.get("checkSuite") or {}).get("workflowRun") or {}
    return (run.get("workflow") or {}).get("name", "")


def started(node: dict[str, Any]) -> str:
    """When a check run started, or a status was set; ISO 8601, so it orders as text."""
    return node.get("startedAt") or node.get("createdAt") or ""


# the environment variable naming the local deny list (denied), a file kept outside the repo
DENY_LIST = "LOOP_DENY_LIST"


def denied(text: str) -> None:
    """Refuse to post `text` when it names a term of the local deny list: the file `LOOP_DENY_LIST` names, one term per
    line (a name, a repo slug, a hostname; `#` starts a comment), matched ignoring case at word boundaries, since a
    public repo's comment keeps its first text in GitHub's edit history for good. Without the variable nothing is
    checked; a list it names that can't be read refuses, since an unread list would let every post through."""
    if not (where := os.environ.get(DENY_LIST)):
        return
    try:
        listed = Path(where).expanduser().read_text().splitlines()
    except OSError as exc:
        raise Refused(
            f"{DENY_LIST} names {where}, which can't be read ({exc.strerror}): nothing posted"
        ) from exc
    terms = [term for line in listed if (term := line.split("#", 1)[0].strip())]
    for number, line in enumerate(text.splitlines(), 1):
        for term in terms:
            if re.search(rf"(?<!\w){re.escape(term)}(?!\w)", line, re.IGNORECASE):
                raise Refused(
                    f"refused to post: line {number} names `{term}` from the deny list ({DENY_LIST}); nothing posted"
                )


def closing_issues(pull: dict[str, Any]) -> list[dict[str, Any]]:
    """GitHub's closing references into the PR's own repo plus every `#n` after a closing keyword, `Part of` or `Implements`
    in the body: GitHub sometimes never links a closing line, and has no API to set the link. A reference to another
    repo's issue is a different issue, whatever its number (closed()), and stays out of every gate."""
    repo = pull["baseRepository"]["nameWithOwner"]
    issues: list[dict[str, Any]] = [
        issue for issue in pull["closingIssuesReferences"]["nodes"] if in_repo(issue, repo)
    ]
    unlinked = sorted(set(named(pull["body"] or "")) - {issue["number"] for issue in issues})
    if unlinked:  # a `#n` that is a pull request is left out
        issues = issues + issues_of(unlinked)
    return issues


def public(text: str) -> str:
    """`text` as posted: the home directory written `~` and the temp dir `<tmp>`, pytest's `pytest-of-<user>/pytest-<n>`
    under it included, since a path under either (a log's, a cache's, a failing test's) names the person whose machine
    posted it, and GitHub's edit history keeps a comment's first text for good."""
    # the longest temp dir first and home last, since a temp dir can sit under /tmp or under home
    # ponytail: this process's temp dir and /tmp; another TMPDIR's paths stay, with only the account name masked
    tmps = {tempfile.gettempdir(), os.path.realpath(tempfile.gettempdir()), "/tmp"}
    for tmp in sorted(filter(None, (t.rstrip("/") for t in tmps)), key=len, reverse=True):
        text = re.sub(
            r"(?<![\w.~-])" + re.escape(tmp) + r"(?:/pytest-of-[^/\s]+(?:/pytest-\w+)?)?(?![\w.-])",
            "<tmp>",
            text,
        )
    if home := str(Path.home()).rstrip("/"):
        text = re.sub(re.escape(home) + r"(?![\w.-])", "~", text)
    return re.sub(r"pytest-of-[^/\s]+", "pytest-of-<user>", text)


class Refused(Exception):
    """A reason to stop with exit 1, printed as is."""


class Drifted(Refused):
    """judge_with_base's refusal of code or policy here that is not `origin/<base>`'s, which `land --wait` can catch up on."""

    def __init__(self, message: str, base: str) -> None:
        super().__init__(message)
        self.base = base


# Ports: the policy reaches the tracker, the forge and the evidence only through these operations, each a function of
# an adapter's `tracker`, `forge` or `evidence` (github_adapter.py's are the reference). An item is an issue or a PR by
# number; reads return nodes shaped as github_adapter.py's ISSUE and PULL queries read them, and a failed call raises
# subprocess.CalledProcessError with the tool's reason as its stderr. An approval record, a verdict's review and a
# check pass's comment are notes on the item the node's `comments` and `reviews` hold; a pass is also a record branch
# on origin, which is git (publish), and a commit status (evidence).
# ponytail: GitHub's node shapes are the data model; give the ports their own when a second adapter needs them.
PORTS = {
    # issue_node(n) (an issue, Refused for a PR), issues(numbers) (the issues among them), comment(n, body),
    # label(n, name, add), assign(n, login, add), close_issue(n)
    "tracker": ("issue_node", "issues", "comment", "label", "assign", "close_issue"),
    "forge": (
        "pull_request",  # (pr) -> the PR node, with its reviews, threads, checks, comments and closing issues
        "pull_status",  # (pr) -> {state, baseRefName, headRefOid, mergeable} alone, for a caller that reads no more
        "queue_watch",  # (prs) -> {pr: {state, headRefOid, isInMergeQueue, labels}}: a queue wait's poll, one call
        "current_pr",  # () -> the checkout's branch's open PR number, or None
        "pr_changes",  # (pr) -> each file the PR changes, with its patch
        "merged_pull",  # (pr) -> the merged PR with its head and merge commit, or None
        "merge_base",  # (base, head) -> the commit the PR's diff starts from
        "diff_lines",  # (base, head) -> per file, the lines a review at head can comment on
        "tree",  # (sha) -> {path: (mode, blob)} of the commit, or None when too large
        "head_pushed",  # (pull) -> when the head was pushed to the PR's branch, or None
        "merged_labelled",  # (branch, name) -> the merged PRs into branch carrying the label
        "open_pulls",  # (limit) -> up to limit open PRs, each with its number, branches, draft, body and closing issues
        "open_pull_files",  # (limit) -> up to limit open PRs, each with its number, base, head commit and changed paths
        "open_heads",  # () -> the head commit of every open PR
        "required_checks",  # (base) -> the checks the base requires, `queue` the merge method of a merge queue its
        # rules require (None, or no such attribute, without one)
        "default_branch",  # () -> the repository's default branch
        "viewer",  # () -> the account running this, or None when the forge won't say
        "writer",  # (login) -> whether the account can write to the repo
        "github_repo",  # (remote URL) -> the `owner/repo` it names on this forge, lowercased, or None
        "one_repo",  # () -> Refused when the checkout can't tell which repo its calls reach
        "ready",  # (pr, undo=False) -> marks a draft ready, or a ready PR a draft
        "merge",  # (pr, method, head) -> merges pinned to head, with the forge's default body
        "enqueue_pull",  # (pr, node, head, method) -> puts the PR in its base's merge queue pinned to head; its position, or None
        "dequeue_pull",  # (pr, node) -> takes the PR out of the merge queue
        "run_info",  # (run) -> an Actions run: repository, workflow_id, head_sha, conclusion, completed times
        "run_jobs",  # (run) -> its jobs, each with name, conclusion and steps
        "workflow_runs",  # (workflow, branch=None, head=None) -> the newest completed runs of a workflow
        "run_artifact",  # (run, name) -> the JSON in the run's artifact, or None
        "compare",  # (base, head) -> {status, ahead_by, behind_by} of head against base
        "commit_checks",  # (ref) -> {name: {conclusion, url, id, title, summary}} of the newest run of each check on the commit
        "job_log",  # (job) -> a check run's job log
        "commit_tree",  # (sha) -> the commit's tree SHA
        "repo_tags",  # (repo) -> {name: commit} of every tag of `repo`
        "tag_tree",  # (repo, tag) -> (commit, {path: (mode, blob)}) of tag `tag` of `repo`, or None when it has none
        "file_at",  # (ref, path, repo=None) -> the file's text at ref, of `repo` or this one, or None when absent
        "pusher",  # (ref) -> {after, login, app} of the newest push to the branch `ref`, `app` true for an app's account, or None
    ),
    # a commit's statuses, newest first, each with its context, state, description, creator and creation time
    # (statuses(sha)); one set (post_status(sha, context, description, state="success")); a verdict posted as a review
    # on its commit (post_review(pr, review)); an outdated note collapsed (minimize(id))
    "evidence": ("statuses", "post_status", "post_review", "minimize"),
}


def sibling(name: str) -> ModuleType:
    """The adapter module `<name>.py` beside this file, loaded by path, so two copies of the gatekeeper in one process
    (a project's and an installed one) each load their own; then given this file's Refused, public and denied, so a
    refusal it raises exits 1 and every text it posts is masked and checked as the gatekeeper's own."""
    spec = importlib.util.spec_from_file_location(
        f"{__name__}_{name}", Path(__file__).with_name(f"{name}.py")
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.Refused, module.public, module.denied = Refused, public, denied
    module.OWN_CHECKS = frozenset(OWN)
    return module


github = sibling("github_adapter")
# every comment the loop writes: a heading, a short summary and the rest folded (folding.py)
folding = sibling("folding")
fold, machine = folding.fold, folding.machine
# the adapters a policy's `adapters` section may name, by name; another team's adapter is added here
ADAPTERS: dict[str, Any] = {"github": github}
# each port's adapter while main() runs, read from the policy at the first call (port); GitHub's outside main()
using: dict[str, Any] | None = None


def adapters(policy: str | None) -> dict[str, Any]:
    """Each port's adapter, by the policy's `adapters` names (`github` for one it doesn't name); a name no adapter
    has is refused, naming it (settings() refuses it first), and so is an adapter lacking an operation of its port."""
    named = settings(policy).get("adapters", {})
    chosen = {}
    for name, operations in PORTS.items():
        adapter = named.get(name, "github")
        if adapter not in ADAPTERS:
            raise Refused(unknown_adapter(source(policy), name, adapter))
        chosen[name] = getattr(ADAPTERS[adapter], name, None)
        if missing := [op for op in operations if not hasattr(chosen[name], op)]:
            raise Refused(f"the {adapter} adapter's {name} port lacks {', '.join(missing)}")
    return chosen


def unknown_adapter(file: str, port_name: str, adapter: Any) -> str:
    return (
        f"{file}'s `adapters.{port_name}` names {adapter!r}, which no adapter implements:"
        f" {', '.join(sorted(ADAPTERS))}"
    )


def port(name: str) -> Any:
    """Port `name`'s adapter: GitHub's outside main(); in it, the default branch's policy's, read once at the first call."""
    global using
    if not running:
        return getattr(github, name)
    if using is None:
        # outside a checkout there is no policy, and the adapter says why it can't run
        try:
            toplevel()
        except Refused:
            using = adapters(None)
        else:
            # the default branch's policy whatever policy_from the first call sits in: the adapters are chosen before any
            # PR is read, so no PR's base can choose them, and a run's adapters don't depend on which call came first
            with policy_from(None):
                using = adapters(project()[1])
    return using[name]


# whether main() runs, so a port reads the policy's `adapters`; a test calling a function alone gets GitHub's
running = False


def through(name: str, operation: str) -> Callable[..., Any]:
    """`operation` of port `name`, called on the adapter in use at the call."""
    return lambda *args, **kwargs: getattr(port(name), operation)(*args, **kwargs)


issue_node, issues_of = through("tracker", "issue_node"), through("tracker", "issues")
label, assign = through("tracker", "label"), through("tracker", "assign")
close_issue = through("tracker", "close_issue")
pull_request, current_pr = through("forge", "pull_request"), through("forge", "current_pr")
queue_watch = through("forge", "queue_watch")
pull_status = through("forge", "pull_status")
pr_changes, merged_pull = through("forge", "pr_changes"), through("forge", "merged_pull")
tag_tree, file_at = through("forge", "tag_tree"), through("forge", "file_at")
repo_tags = through("forge", "repo_tags")
merge_base, diff_lines = through("forge", "merge_base"), through("forge", "diff_lines")
tree, head_pushed = through("forge", "tree"), through("forge", "head_pushed")
required_checks, default_branch = (
    through("forge", "required_checks"),
    through("forge", "default_branch"),
)
viewer, writer = through("forge", "viewer"), through("forge", "writer")
github_repo, one_repo = through("forge", "github_repo"), through("forge", "one_repo")
open_heads, open_pull_files = through("forge", "open_heads"), through("forge", "open_pull_files")
enqueue_pull, dequeue_pull = through("forge", "enqueue_pull"), through("forge", "dequeue_pull")
commit_checks, job_log = through("forge", "commit_checks"), through("forge", "job_log")
pusher = through("forge", "pusher")
run_info, run_jobs = through("forge", "run_info"), through("forge", "run_jobs")
workflow_runs, run_artifact = through("forge", "workflow_runs"), through("forge", "run_artifact")
compare = through("forge", "compare")
commit_tree = through("forge", "commit_tree")
statuses, post_status = through("evidence", "statuses"), through("evidence", "post_status")
post_review, minimize = through("evidence", "post_review"), through("evidence", "minimize")


comment = through("tracker", "comment")


def merged_high(branch: str) -> list[int]:
    """The merged PRs into `branch` that carry `risk:high` (flag() adds it to each high-risk PR that lands)."""
    found: list[int] = port("forge").merged_labelled(branch, RISK_LABEL)
    return found


def open_pulls() -> list[dict[str, Any]]:
    """The repo's open PRs, up to OPEN_LIMIT, each with its number, branch, base, whether it is a draft, body and
    closing references (each naming its issue's repo as `nameWithOwner`, as the PR query does), in one call."""
    found: list[dict[str, Any]] = port("forge").open_pulls(OPEN_LIMIT)
    return found


def read(path: Path) -> str:
    try:
        return path.read_text()
    except OSError as exc:
        raise Refused(f"{path}: {exc.strerror}") from exc


def toplevel() -> Path:
    """The checkout's root, so a run from a subdirectory reads the same files."""
    try:
        return Path(git("rev-parse", "--show-toplevel").strip())
    except subprocess.CalledProcessError as exc:
        raise Refused("not in a git checkout: run from the repo's checkout") from exc


def working_tree(root: Path) -> bool:
    """Whether the clone at `root` has its files checked out: False when HEAD names a commit and there is no index, as
    in a `--no-checkout` clone or worktree that a host keeps only to launch cloud sessions from."""
    index = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--path-format=absolute", "--git-path", "index"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    born = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--verify", "-q", "HEAD^{commit}"],
        capture_output=True,
        check=False,
    )
    return born.returncode != 0 or not index or Path(index).exists()


@functools.cache
def policy_ref(root: Path) -> str | None:
    """Where the policy files are read: None in a clone with a working tree, whose files they are (judge_with_base
    refuses a land or merge check where they differ from the base's); in one without, origin's default branch's
    remote-tracking ref, fetched once per run, since its files are the ones main holds and the host needs no checkout.
    A failed fetch is refused, since the ref as last fetched may hold an older policy than main's now."""
    if working_tree(root):
        return None

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True, check=False
        )

    # origin/HEAD as the clone recorded it, else as origin says now
    name = run("symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD").stdout.strip()
    name = name.removeprefix("origin/") or "".join(
        re.findall(
            r"^ref: refs/heads/(\S+)\tHEAD$",
            run("ls-remote", "--symref", "origin", "HEAD").stdout,
            re.MULTILINE,
        )[:1]
    )
    if not name:
        raise Refused(
            f"{root} has no working tree and origin names no default branch, so there is no policy to read:"
            " run where the files are checked out"
        )
    ref = f"refs/remotes/origin/{name}"
    if run("fetch", "-q", "origin", f"+refs/heads/{name}:{ref}").returncode:
        raise Refused(
            f"{root} has no working tree and origin/{name} could not be fetched, so the policy would be the last"
            " fetched one: retry, or run where the files are checked out"
        )
    return ref


def shown_at(root: Path, ref: str, path: Path) -> bytes | None:
    """`path`'s bytes at `ref`, None where the ref holds no such file."""
    found = subprocess.run(
        ["git", "-C", str(root), "show", f"{ref}:{path.as_posix()}"],
        capture_output=True,
        check=False,
    )
    return found.stdout if found.returncode == 0 else None


def policy_bytes(root: Path, path: Path) -> bytes | None:
    """A policy file's bytes as this run reads it (policy_ref): the working tree's, else origin's default branch's."""
    if (ref := policy_ref(root)) is not None:
        return shown_at(root, ref, path)
    try:
        return (root / path).read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise Refused(f"{root / path}: {exc.strerror}") from exc


# the base the PR a command acts on merges onto, which project() reads the policy from (policy_from); None: the
# default branch, for a command with no PR
POLICY_BASE: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "POLICY_BASE", default=None
)


@contextlib.contextmanager
def policy_from(base: str | None) -> Any:
    """Read the policy from `origin/<base>` for what runs inside, every project() call down to gate() and gate_plan():
    the entry point that has a PR (check, land, local-ci <pr>, batch, go, delegate, brief) sets its base once, so no
    caller looks it up again. None leaves the default branch."""
    token = POLICY_BASE.set(base)
    try:
        yield
    finally:
        POLICY_BASE.reset(token)


def project(*, base: str | None = None) -> tuple[str | None, str | None]:
    """The base's docs/agents/issue-tracker.md and its policy, None for one it lacks: a repo without the files has no
    rules, and its issues no label shape. Each file is read from `origin/<base>` (policy_base), the ref `land` builds
    its merge on: `base`, else the one policy_from set, else the default branch, so a PR can't loosen its own gate and a checkout behind its base can't hold an old one; a copy in the
    working tree that differs gets one stderr line naming the file and both commits, and the base's is used. Without
    that ref (no origin) the working tree's are read, with one stderr line saying so. The policy is gatekeeper.md's (a
    Policy), else loop.md's `## Settings` block, read with a deprecation line; a project with both is refused, since
    two sources of the bar would drift. Settings off their schema are refused here, before any proof is printed or any
    PR touched."""
    root, base = toplevel(), base or POLICY_BASE.get()
    tracker, loop, policy = (policy_file(root, path, base) for path in (TRACKER, LOOP, POLICY))
    loop = chosen(loop, policy)
    if tracker is not None:
        tracker_settings(tracker)
    return tracker, loop


def policy_file(root: Path, path: Path, base: str | None = None) -> str | None:
    """`path` as project() reads it: from `origin/<base>` (policy_base), None where the base lacks it, printing
    `differs` once when the working tree's copy is another; from the working tree, printing `unbased` once, when there
    is no such ref."""
    policy_ref(
        root
    )  # a clone with no working tree: origin's default branch fetched once, a failed fetch refused
    found = policy_base(str(root), base)
    held = read(root / path) if (root / path).exists() else None
    if found is None:
        if not working_tree(root):  # no files to fall back to: empty policy would judge by defaults
            raise Refused(
                f"{root} has no working tree and no origin/{base or default_base(str(root))} ref, so there is no"
                " policy to read: fetch that base, or run where the files are checked out"
            )
        unbased(str(root))
        return held
    ref, tip = found
    shown = subprocess.run(
        ["git", "-C", str(root), "show", f"{tip}:./{path.as_posix()}"],
        capture_output=True,
        text=True,
        check=False,
    )
    based = shown.stdout if shown.returncode == 0 else None
    if held != based and working_tree(root):  # with no working tree there is no copy to differ
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        differs(path.as_posix(), head or "no commit", ref, tip)
    return based


# set to any value, project() reads the working tree's policy files, never `origin/<base>`'s: for tests and previews
# that edit the policy after opening PRs. A real run leaves it unset, so a PR cannot loosen its own gate.
WORKING_TREE_POLICY = "GATEKEEPER_POLICY_FROM_WORKING_TREE"


def policy_base(root: str, base: str | None = None) -> tuple[str, str] | None:
    """The ref the policy is read from in the checkout at `root`, `origin/<base>` (`base`, else its default branch:
    default_base), and that ref's commit now, since `land` fetches it during a run; None without such a ref."""
    if os.environ.get(
        WORKING_TREE_POLICY
    ):  # a test or preview that edits the policy in the working tree
        return None
    base = base or default_base(root) or ""  # none: `refs/remotes/origin/` names no commit
    tip = subprocess.run(
        [
            "git",
            "-C",
            root,
            "rev-parse",
            "--verify",
            "--quiet",
            f"refs/remotes/origin/{base}^{{commit}}",
        ],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    return (f"origin/{base}", tip) if tip else None


@functools.cache
def default_base(root: str) -> str | None:
    """The default branch of the checkout at `root`, as base_branch finds it (origin/HEAD, else what `origin` says its
    HEAD is), None when neither answers. Once per run, since project() is read many times and asking origin is a
    network call."""

    def ask(*args: str) -> str:
        done = subprocess.run(
            ["git", "-C", root, *args], capture_output=True, text=True, check=False
        )
        return done.stdout.strip() if done.returncode == 0 else ""

    if base := ask("symbolic-ref", "--short", "refs/remotes/origin/HEAD").removeprefix("origin/"):
        return base
    for line in ask("ls-remote", "--symref", "origin", "HEAD").splitlines():
        if line.startswith("ref: refs/heads/"):
            return line.split("\t")[0].removeprefix("ref: refs/heads/")
    return None


@functools.cache
def unbased(root: str) -> None:
    """One line per run and checkout, however often the policy is read; none for a directory that is no checkout (a
    test's, since toplevel() refuses one), which has no base to fall back from."""
    inside = subprocess.run(
        ["git", "-C", root, "rev-parse", "--is-inside-work-tree"], capture_output=True, check=False
    )
    if inside.returncode:
        return
    why = (
        f"{WORKING_TREE_POLICY} is set"
        if os.environ.get(WORKING_TREE_POLICY)
        else "no origin/<base> ref"
    )
    print(f"approvals.py: {why}, so the policy is read from the working tree", file=sys.stderr)


@functools.cache
def differs(path: str, head: str, ref: str, tip: str) -> None:
    """One line per run, file and pair of commits, however often the policy is read."""
    print(
        f"approvals.py: {path} in the working tree (at {head}) differs from {ref}'s (at {tip}): using {ref}'s",
        file=sys.stderr,
    )


def chosen(loop: str | None, policy: str | None) -> str | None:
    """The policy of a loop.md and a gatekeeper.md's texts, None for one missing (project): gatekeeper.md's (a Policy),
    else loop.md's `## Settings` block, read with a deprecation line; both holding one is refused, since two sources of
    the bar would drift. Settings off their schema are refused here."""
    if policy is not None:
        if loop is not None and SETTINGS.search(section(loop, "Settings")):
            raise Refused(
                f"both {POLICY} and {LOOP}'s ## Settings hold a policy: keep it in {POLICY} alone"
            )
        loop = Policy(policy)
    elif loop is not None:
        moved()
    settings(loop)
    return loop


def base_policy(base: str, checkout: str | None) -> str | None:
    """The policy on `origin/<base>` after a fetch of it, chosen from its files as project() chooses the default
    branch's, since a branch's own edit of the policy is not the one the owner approved; `checkout`, project()'s, when
    that ref holds neither file (or no fetch ever made it), today's rule. A failed fetch reads the ref as last fetched."""
    ref = f"refs/remotes/origin/{base}"
    root = toplevel()
    subprocess.run(
        ["git", "-C", str(root), "fetch", "-q", "origin", f"+refs/heads/{base}:{ref}"],
        capture_output=True,
        check=False,
    )

    def shown(path: Path) -> str | None:
        found = subprocess.run(
            ["git", "-C", str(root), "show", f"{ref}:{path.as_posix()}"],
            capture_output=True,
            text=True,
            check=False,
        )
        return found.stdout if found.returncode == 0 else None

    loop, policy = shown(LOOP), shown(POLICY)
    if loop is None and policy is None:
        return checkout
    return chosen(loop, policy)


@functools.cache
def moved() -> None:
    """One line per run, however often the policy is read."""
    print(
        f"{LOOP}'s ## Settings is deprecated: move the block to {POLICY} under ## Policy (the gatekeeper skill's policy.md)",
        file=sys.stderr,
    )


def adopted(issues: list[dict[str, Any]], loop: str | None) -> list[dict[str, Any]]:
    """adopting, recording: the issues after each owner's label it finds is recorded as their approval."""
    return adopting(issues, loop, record=True)


def adopting(issues: list[dict[str, Any]], loop: str | None, record: bool) -> list[dict[str, Any]]:
    """The issues, each read again after recording an `approved:<point>` label the owner (gatekeeper.md's `owner`) added
    after the spec's or plan's last edit as their approval (approve), which also removes `needs-owner`, printing a line
    for each; a label the owner added before that edit gets a line saying so and changes nothing, nor does anyone
    else's. Without `owner` nothing is read, since only the owner's own label is their word. Without `record`, the
    same decision read-only, for a command that writes nothing (rerun_state, under a CI token that can't write): no
    approval is recorded and nothing printed, and each issue it would record comes back as the record leaves its
    labels, `needs-owner` gone."""
    owner = settings(loop).get("owner")
    if not owner:
        return issues
    found = []
    for issue in issues:
        number, recorded = issue["number"], False
        for point in POINTS:
            holder = spec_holder(issue) if point == "spec" else headed(issue, "## Plan")
            name = f"approved:{point}"
            if holder is None or name not in names(issue):
                continue
            value = texted(holder)
            if current(issue, point, point, versions(holder), "owner") or current(
                issue, point, point, versions(holder), DELEGATE
            ):
                # the delegate's record already stands, and its own label rides the owner's account: not the owner's word
                continue
            added = [
                event
                for event in events(issue, "LabeledEvent")
                if (event.get("label") or {}).get("name") == name
            ]
            # GitHub logins are case-insensitive: `Octocat` in gatekeeper.md names the account `octocat`
            if (
                not added
                or (added[-1].get("actor") or {}).get("login", "").lower() != str(owner).lower()
            ):
                continue
            if added[-1]["createdAt"] <= last_change(holder):
                if not record:
                    continue
                print(
                    f"#{number}: the owner's `{name}` was added before the {point}'s last edit ({version(holder)}): the"
                    " owner adds it again"
                    if "lastEditedAt" in holder
                    else f"#{number}: the owner's `{name}` can't be dated after the {point}'s last edit, since GitHub's"
                    f" REST API gives no edit time and the {point} changed at {last_change(holder)}, after it: the owner"
                    f" records it with `approve {point} {number} --by owner`, or the check runs where GraphQL answers"
                )
                continue
            if not record:
                labels = [
                    label for label in issue["labels"]["nodes"] if label["name"] != NEEDS_OWNER
                ]
                issue = {**issue, "labels": {**issue["labels"], "nodes": labels}}
                continue
            approve(point, number, value)
            recorded = True
            print(
                f"#{number}: recorded the owner's `{name}`, added after the {point}'s last edit, as their approval"
            )
        found.append(issue_node(number) if recorded else issue)
    return found


def gated(
    point: str, pull: dict[str, Any], tracker: str | None, loop: str | None
) -> tuple[list[str], list[dict[str, Any]]]:
    """Every missing proof at `point`, and the issues the PR names (none for an exact revert, which needs no spec: risk()
    reads its issues from `named` and the reverted PR's risk from `reverted` instead). At merge, a PR that itself carries
    `needs-owner` waits for the owner, and one that carries `loop:hold` is held by its lead or another coordinator, so no
    batch takes either."""
    held = (
        [f"#{pull['number']} waits for the owner ({NEEDS_OWNER})"]
        if point == "merge" and NEEDS_OWNER in names(pull)
        else []
    ) + (
        [f"#{pull['number']} is held ({HOLD}): see the hold comment on the PR"]
        if point == "merge" and HOLD in names(pull)
        else []
    )
    revert, found = reverts(pull)
    found = held + found
    if revert:
        pull["named"], pull["reverted"] = closing_issues(pull), reverted(revert)
        return found + proofs(point, pull, [], loop, revert), []
    issues = adopted(closing_issues(pull), loop)
    # a mechanical bump names no issue: there is no spec to build from
    if issues or not machine_made(pull, loop):
        found += problems(issues, None if tracker is None else label_names(tracker))
    found += closes_nothing(pull, issues, loop)
    return found + proofs(point, pull, issues, loop, revert), issues


def machine_made(pull: dict[str, Any], loop: str | None) -> bool:
    """Whether the PR is a mechanical bump the gate itself proves (#2040): a pack bump (mechanical: a `bot` section's
    `branch` PR whose `apm.yml` moves only the pack's `ref:` line, the rest its lock and generated files) or a pin to a
    released pack tag byte for byte (pin_target). Such a PR names no issue and needs none. A `branch` section alone
    is not enough, since it vouches only for the paths, not the change; and the release fold, which only CI's
    `release-verify` checks, still names its issue. Read only when the PR names no issue, since both read the forge."""
    return mechanical(pull, loop) is None or pin_target(pull) is not None


def closes_nothing(
    pull: dict[str, Any], issues: list[dict[str, Any]], loop: str | None
) -> list[str]:
    """A line when the PR closes no open issue: GitHub lists a PR in an issue's Development panel (and the issue in
    the PR's) only for a closing keyword, so a PR that only says `Part of #n` shows on no issue. A slice of an epic closes
    its own sub-issue, never the epic. A PR naming no issue at all is problems()'s refusal, not repeated here; a revert has its own path (gated),
    and a verified release pin bump (pin_target) and a PR a `bot` section covers (exempting: the release workflow's
    fold, whose `Part of` names a standing release epic that stays open across releases) are exempt."""
    if not issues:
        return []
    mine = closed(pull, pull["baseRepository"]["nameWithOwner"], loop)
    if any(i["number"] in mine and str(i.get("state", "OPEN")).upper() == "OPEN" for i in issues):
        return []
    if pin_target(pull) is not None:  # a verified release pin bump has no issue to close
        return []
    # a bot's PR, the release fold among them, finishes no issue of its own
    if exempting(pull, loop) is not None:
        return []
    return [
        (
            "the PR closes no open issue: GitHub lists a PR on an issue's Development panel only for a closing keyword, so"
            " `Part of` alone leaves it on none. Add `Closes #<n>` for the issue it finishes; a slice of an epic gets its own"
            " sub-issue to close, never the epic"
        )
    ]


def reverted(number: int) -> str | None:
    """Why an exact revert of #number is high risk for what it reverts, or None: `risk:high` on #number, or #number's
    last verdict judging it high (judged), since undoing a high-risk change (or re-landing one, a revert of its
    revert) changes as much as making it."""
    original = pull_request(number)
    if RISK_LABEL in names(original):
        return f"`{RISK_LABEL}` is on #{number}, which it reverts"
    if why := judged(original):
        return f"it reverts #{number}, and {why}"
    return None


# what GitHub says of a PR number no PR has: GraphQL's refusal, REST's 404
NO_PULL = ("Could not resolve to a PullRequest", "Not Found")


# the pack's scripts as a repo's mise tasks and workflows name them, and the one way each must not be run: a bare
# python, which has no PyYAML; `uv run` (or the script's own shebang) installs it
# a pack script's file name, which must start its path component (after the start, whitespace, `/` or a quote,
# the only real starts), so a consumer's own `pr_board.py`, `my:board.py` or `my+board.py` is not the pack's
PACK_NAME = r"(?<![^\s/'\"])(?:approvals|gatekeeper|labels|board)\.py\b"
PACK_SCRIPT = re.compile(PACK_NAME)
BARE_PYTHON = re.compile(r"\bpython[\d.]*\s+(?:-\S+\s+)*\S*?" + PACK_NAME)
MISE_CONFIGS = (
    "mise*.toml",
    ".mise*.toml",
    ".config/mise*.toml",
    ".config/mise/*.toml",
    "mise/config*.toml",
    ".mise/config*.toml",
)
MISE_TASK_DIRS = ("mise-tasks", ".mise-tasks", ".mise/tasks", ".config/mise/tasks", "mise/tasks")


def mise_files(root: Path) -> list[Path]:
    """The mise configs and file tasks of the repo, in every place mise reads them."""
    configs = {path for pattern in MISE_CONFIGS for path in root.glob(pattern) if path.is_file()}
    tasks = {path for name in MISE_TASK_DIRS for path in (root / name).rglob("*") if path.is_file()}
    return sorted(configs | tasks)


def flat(body: object) -> str:
    """A mise task's text (a string, or a table or list holding strings) as lines."""
    if isinstance(body, dict):
        body = list(body.values())
    return "\n".join(map(flat, body)) if isinstance(body, list) else str(body)


def runs_task(text: str, task: str) -> bool:
    """Whether a `mise run` command in the text names the task; a command ends at a newline or a shell operator."""
    return bool(re.search(rf"mise run\b[^\n;&|]*[ '\"]{re.escape(task)}\b", text))


def pack_tasks(root: Path) -> set[str]:
    """The names of the mise tasks that run a pack script, directly or through `mise run <task>` of one that does:
    `[tasks.<name>]` tables, the `[tasks]` table form, and file tasks (named by their path, `/` as `:`).
    Needs tomllib (Python 3.11+), imported here so the other commands still start on the pack's 3.9."""
    import tomllib

    bodies: dict[str, str] = {}
    for path in mise_files(root):
        if path.suffix == ".toml":
            try:
                tasks = tomllib.loads(read(path)).get("tasks") or {}
            except tomllib.TOMLDecodeError:
                continue
            for task, body in tasks.items():
                bodies[task] = bodies.get(task, "") + flat(body)
        else:
            folder = next(d for d in MISE_TASK_DIRS if (root / d) in path.parents)
            task = ":".join(path.relative_to(root / folder).parts)
            bodies[task] = bodies.get(task, "") + read(path)
    named: set[str] = set()
    while grew := {
        task
        for task, body in bodies.items()
        if task not in named
        and (PACK_SCRIPT.search(body) or any(runs_task(body, other) for other in named))
    }:
        named |= grew
    return named


def setup_problems(root: Path) -> list[str]:
    """Where the repo's own loop steps would exit non-zero for a reason the policy parse can't show: a pack script run
    by a bare python in a mise config or workflow (no PyYAML), and a workflow job that runs one through a mise task
    after `jdx/mise-action` was told not to install (`install: false`, or `install_args` without uv), so there is no
    `uv`. ponytail: a text scan of mise.toml and .github/workflows, not a run of each step, which needs the forge;
    the upgrade is `check setup --run`, executing each task against a stub forge."""
    try:
        tasks = pack_tasks(root)
    except ImportError:
        refusal = (
            "check setup reads mise's TOML with tomllib, which needs Python 3.11 or later: "
            "run `uv run --python '>=3.11' <path to>/approvals.py check setup`"
        )
        return [refusal]
    found = []
    workflows = sorted((root / ".github" / "workflows").glob("*.y*ml"))
    for path in mise_files(root) + workflows:
        for number, line in enumerate(read(path).splitlines(), 1):
            if BARE_PYTHON.search(line):
                found.append(
                    f"{path.relative_to(root)}:{number}: `{line.strip()}` runs a pack script with a bare python, "
                    "which has no PyYAML: run it with `uv run <script>`"
                )
    for path in workflows:
        try:
            jobs = (yaml.safe_load(read(path)) or {}).get("jobs") or {}
        except yaml.YAMLError:
            continue
        for job, body in jobs.items():
            steps = body.get("steps") or [] if isinstance(body, dict) else []
            withs = [
                step.get("with") or {}
                for step in steps
                if "mise-action" in str(step.get("uses", ""))
            ]
            skipped = any(
                str(w.get("install", True)).lower() == "false"
                or ("install_args" in w and not re.search(r"\buv\b", str(w["install_args"])))
                for w in withs
            )
            runs = "\n".join(str(step.get("run", "")) for step in steps)
            runs_pack = PACK_SCRIPT.search(runs) or any(runs_task(runs, task) for task in tasks)
            # another step that installs uv (mise install, setup-uv) makes the skip harmless; step order is not read
            installs_uv = any(
                "setup-uv" in str(step.get("uses", ""))
                or re.search(r"\bmise install\b", str(step.get("run", "")))
                for step in steps
            )
            if skipped and runs_pack and not installs_uv:
                found.append(
                    f"{path.relative_to(root)}: job `{job}` runs a pack script but mise-action installs no uv "
                    "(`install: false`, or `install_args` without uv): set `install_args: uv`"
                )
    return found


def setup() -> int:
    """`check setup`, the gate a consumer runs on a pack upgrade: reads the policy as the loop steps do
    (project(): refused when it is the bullet form or off its schema), then the repo's own invocations
    (setup_problems). Exit 0 when every loop step would start, else 1 with one line per problem."""
    root = toplevel()
    problems: list[str] = []
    try:
        project()
    except Refused as refusal:
        problems.append(str(refusal))
    problems += setup_problems(root)
    try:
        if drift := mergify_drift(root, tree_policy(root), whole=False):
            problems.append(drift)
    except Refused:
        pass  # the policy's own refusal is already listed above
    for line in problems:
        print(line)
    if not problems:
        print("setup: the policy parses and the repo's loop steps run the pack's scripts with uv")
    for line in session_notice():  # advice, never a problem: the field is optional
        print(line)
    return 1 if problems else 0


def check(point: str, pr: int | None, issue: int | None = None) -> int:
    """`check <point>`: with a PR, under the policy of the base it merges onto (policy_from), else the default
    branch's."""
    toplevel()  # outside a checkout, refused before GitHub is asked
    if point == "stalled":
        return stalled()
    if point == "rerun":
        if pr is None:
            raise Refused("check rerun needs a PR: it reads that PR's rating")
        pull = pull_request(pr)
        with policy_from(pull["baseRefName"]):
            return rerun_state(pull)
    if point == "build":
        full_check_line()
    if issue is not None:
        return unbuilt(issue, *project())
    if pr is None and (pr := current_pr()) is None:
        project()  # a policy off its schema is refused here too
        if point == "build":
            if (issue := branch_issue()) is None:
                print(
                    "no PR for this branch yet, and its name holds no issue number: pass --issue <n>, since a build"
                    " needs an approved spec"
                )
                return 1
            return unbuilt(issue, *project())
        print("no PR for this branch yet: nothing to gate")
        return 0
    try:
        pull = pull_request(pr)
    except subprocess.CalledProcessError as exc:
        if point == "build" and any(text in (exc.stderr or "") for text in NO_PULL):
            return unbuilt(
                pr, *project()
            )  # a number no PR has is the issue's, as `--issue` reads it
        raise
    with policy_from(pull["baseRefName"]):
        return checked(point, pull)


def full_check_line() -> None:
    """The one line `check build` says, on stderr and never fatal, when loop.md has no `Affected tests:` line, so a
    project running the full check at every gate sees why; nothing with the opt-out `Gate: full`, which chose it."""
    with contextlib.suppress(Refused, OSError, subprocess.CalledProcessError):
        text = loop_md()
        if not AFFECTED.search(text) and not FULL_GATE.search(text):
            print(
                "loop.md has no `Affected tests:` line, so every gate runs the full check: name the project's"
                " selector there (SKILL.md, Adopting the loop)",
                file=sys.stderr,
            )


def checked(point: str, pull: dict[str, Any]) -> int:
    tracker, loop = project()
    if (
        point == "ready"
    ):  # the PR body alone: the author's step before the verifier, when no verdict or gate exists yet
        waived = incubation(pull, adopted(closing_issues(pull), loop), loop)[0]
        found = [] if "evidence" in waived else no_evidence(pull) + demo_form(pull)
        print(
            "\n".join(found)
            if found
            else f"PR #{pull['number']}: its body holds the evidence proof"
        )
        return 1 if found else 0
    if point == "merge" and (why := outside(pull, loop)):
        print(why)
        return 0
    if point == "merge":
        base = pull["baseRefName"]
        judge_with_base(base, fetch=True)
    found, issues = gated(point, pull, tracker, loop)
    # flag() first, since a `risk:high` it takes off no longer holds the merge
    after = flag(pull, issues, loop) if point == "merge" else [] if found else claim(pull, issues)
    covered = breakglass_cover(pull, loop, ran(pull)) if point == "merge" else Covered([], [])
    waiting = waits(pull, issues, loop, checks=covered.lines) if point == "merge" else []
    rated = rating(pull, issues, loop) if point == "merge" else None
    if rated is not None:
        # on stderr, so the lines a workflow or a lead reads as the gate's answer stay what holds the merge
        print(rated.line(pull), file=sys.stderr)
    used = (
        whose(pull, loop)
        if rated is not None and not found and not waiting and rated.merge != "gates"
        else []
    ) + covered.said
    # advice, so it prints whatever else holds the merge and changes no exit code
    advice = (
        [f"PR #{pull['number']}: {said}"] if point == "merge" and (said := unforced(pull)) else []
    )
    # what land's pre-land check would refuse, after the rest, so check merge never calls clear a PR land leaves out
    waiting += pre_land_holds(pull, loop, bool(found or waiting)) if point == "merge" else []
    for line in holds(pull, issues, loop) + found + waiting + after + used + advice:
        print(line)
    return 1 if found else WAITING if waiting else 0


def unopened() -> dict[str, Any]:
    """The PR `check build` judges an issue's spec beside before it exists: no labels, and the files the branch has
    changed since it left origin's default branch, as a PR's would be at `check build`; when git can't tell, more files
    than it lists (paths() None), so a `paths` risk rule holds rather than reading as unmatched."""
    try:
        base = git("rev-parse", "--abbrev-ref", "origin/HEAD").strip()
        # NUL-separated, since git quotes a non-ASCII name and a name may hold spaces
        # --no-renames: a moved file lists its old name too, since moving it out of a ruled path changes it
        found = git("diff", "--name-only", "--no-renames", "-z", f"{base}...HEAD").split("\0")[:-1]
    except subprocess.CalledProcessError:
        return {"labels": {"nodes": []}, "files": {"nodes": [], "totalCount": 1}}
    return {"labels": {"nodes": []}, "files": {"nodes": [{"path": path} for path in found]}}


def unbuilt(number: int, tracker: str | None, loop: str | None) -> int:
    """check build on one issue before its PR exists: every build proof a PR's issues need (problems, proofs), judged
    on the issue and the files the branch changes so far (unopened); it claims nothing, since there is no PR to give
    the assignee."""
    issues, pull = adopted([issue_node(number)], loop), unopened()
    found = problems(issues, None if tracker is None else label_names(tracker))
    found += proofs("build", pull, issues, loop)
    for line in holds(pull, issues, loop) + found:
        print(line)
    if not found:
        print(f"#{number}: every build proof holds; no PR yet")
    return 1 if found else 0


def branch_issue() -> int | None:
    """The issue number the current branch's name starts a segment with (`loop/329-build-issue`), or None."""
    found = re.search(r"(?:^|/)(\d+)(?:-|$)", git("rev-parse", "--abbrev-ref", "HEAD").strip())
    return int(found[1]) if found else None


# seconds a merged PR's `ok` event stays readable before its refs are cleared (land_ended)
EVENT_GRACE = 120
# the branches whose `land` event ended (ok, failed, needs-owner) in this round of land, so no beat of the same round
# rewrites an end as `waiting`; and the branches whose `ok` this process has written, so a wait writes it once
ended: set[str] = set()
landed_ok: set[str] = set()
# the PRs the lock's heartbeat beats `waiting` for while a round holds the lock and runs its gates
beating_for: list[dict[str, Any]] = []
# one writer at a time, so a beat in flight cannot land its `waiting` after the `ok` of the same branch
event_lock = threading.Lock()


# events.py beside this script: the loop's event writer, which needs only git
loop_events = sibling("events")


def land_event(
    pull: dict[str, Any], state: str, url: str = "", question: str | None = None
) -> None:
    """Write PR `pull`'s `land` event (events.md) on its branch. An event reports a step and is not part of it, so
    whatever goes wrong here is one line on stderr and changes no exit code of land."""
    try:
        with event_lock:
            if state == "waiting" and pull["headRefName"] in ended:
                return  # its end is written: no beat of this round rewrites it
            if state != "waiting":
                ended.add(pull["headRefName"])
            loop_events.emit(
                "land",
                pull["headRefName"],
                state,
                url=url or pull.get("url") or "",
                question=question,
                cwd=toplevel(),
            )
    except Exception as exc:  # noqa: BLE001 -- by contract nothing here may end a land
        print(
            f"PR #{pull.get('number')}: land event not written: {stderr_of(exc)}", file=sys.stderr
        )


def land_event_of(
    pr: int | Sequence[int] | None,
    state: str,
    question: str | None = None,
    unless_ended: bool = False,
) -> None:
    """land_event for each PR number a land names (the PR is read again, since the wait holds only its numbers);
    `unless_ended` leaves a PR whose end this process wrote, so a later end keeps the first one's reason."""
    for number in numbers_of(pr):
        try:
            pull = pull_request(number)
            if (
                pull["state"]
                == "OPEN"  # one merged during this wait has its `ok`, which a ceiling must not replace
                and not (unless_ended and pull["headRefName"] in ended)
            ):
                land_event(pull, state, question=question)
        except Exception as exc:  # noqa: BLE001 -- the same: a failed read of the PR ends nothing
            print(f"PR #{number}: land event not written: {stderr_of(exc)}", file=sys.stderr)


def land_ended(pull: dict[str, Any]) -> None:
    """A merged PR's `land` event ends `ok`, written once per process, and its refs go (events.py clear). The refs go
    EVENT_GRACE seconds later, in a process of its own, since a native lead blocked in `wait land <branch>` polls and
    would otherwise find nothing where the end was. The clear runs in the repository's common git dir, which outlives
    the checkout this land runs in (caught_up's fresh worktree is removed when the land exits)."""
    branch = pull["headRefName"]
    if branch in landed_ok:  # a wait reads the same merged PR again each round
        return
    landed_ok.add(branch)
    land_event(pull, "ok")
    try:
        script = Path(loop_events.__file__).resolve()
        common = git("rev-parse", "--path-format=absolute", "--git-common-dir").strip()
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import subprocess, sys, time; time.sleep(float(sys.argv[1])); subprocess.run(sys.argv[2:], check=False)",
                str(EVENT_GRACE),
                sys.executable,
                str(script),
                "--repo",
                common,
                "clear",
                branch,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        # ponytail: a detached sleep, not a sweep on the next land, so a branch's refs go without another land; a host
        # that ends within the grace leaves them, and a scheduled `events.py clear` is the upgrade if that happens
    except Exception as exc:  # noqa: BLE001 -- the PR has landed: the refs stay until a `clear` of the branch is run
        print(
            f"PR #{pull.get('number')}: land event refs not cleared: {stderr_of(exc)}",
            file=sys.stderr,
        )


def land(pr: int | Sequence[int] | None, wait: bool = False, only: bool = False) -> int:
    """landing() once, or with `wait` again every POLL seconds while it waits on something time clears, printing each
    new state once: until it ends, or exit 3 at CEILING."""
    if not wait:
        return landing(pr, only)[0]
    global waiting
    waiting = True  # locked() now stamps this land's place in the waiting order
    if place := os.environ.pop(WAIT_ENV, ""):
        since, ident, base, tip = place.split(":")
        waited.update(since=float(since), id=ident, base=base, tip=tip)
    # a stop (SIGTERM, SIGHUP) unwinds the whole wait, so the finally frees this land's place; landing_here sets the same
    # handler for the lock's own release and puts this one back
    previous = {sig: signal.getsignal(sig) for sig in STOPS}
    for sig, handler in previous.items():
        if handler is not signal.SIG_IGN:
            signal.signal(sig, stopped)
    try:
        return wait_loop(pr, only)
    finally:
        for sig, handler in previous.items():
            if handler is not None:
                signal.signal(sig, handler)
        waiting = False
        github.wait_until = (
            None  # the wait is over: gh() no longer guards, nor waits out a rate limit (#1771)
        )
        unwait(forget=True)


def land_wait() -> int:
    """Seconds `land --wait` waits in all: the ceiling the policy's `timeouts` derive (ceiling_of)."""
    _, loop = project()
    return ceiling_of(loop)


def timeouts(loop: str | None) -> dict[str, int]:
    """The policy's `timeouts` in seconds, each key's default (TIMEOUTS, today's values) where it sets none: `test`,
    one test's limit, which a project maps onto its runner's own per-test option in its `Affected tests:` command, and
    `run`, one selection run's, the queue's check included."""
    given = {**TIMEOUTS, **settings(loop).get("timeouts", {})}
    return {key: int(span(text).total_seconds()) for key, text in given.items()}  # type: ignore[union-attr]


def ceiling_of(loop: str | None) -> int:
    """`land --wait`'s ceiling in seconds: the queue's checks timeout (`timeouts.run`) plus a margin of one more run,
    for the batch ahead of the PR in the queue or the one retry of a red batch (mergify.yml's `max_checks_retries:
    1`); 2 hours with nothing set. A project's `land.wait`, deprecated, still wins, so a project that set it after
    #2199 keeps its wait. The landing lock's staleness is CEILING, whatever this is."""
    given = settings(loop).get("land", {}).get("wait")
    if given is not None and (found := span(str(given))):
        return int(found.total_seconds())
    return 2 * timeouts(loop)["run"]


def job_minutes(loop: str | None) -> int:
    """A CI job's `timeout-minutes` for the job that runs one selection: `timeouts.run` in whole minutes, rounded up,
    for the templates that render a project's workflows."""
    return -(-timeouts(loop)["run"] // 60)


def said_span(seconds: int) -> str:
    """A wait's length as the stop message says it, and as Mergify reads a duration: `2 hours`, `4 hours 30 minutes`,
    `90 seconds` (seconds only where a length has them)."""
    hours, minutes = divmod(seconds // 60, 60)
    rest = seconds % 60
    parts = [f"{hours} hour{'' if hours == 1 else 's'}"] if hours or not (minutes or rest) else []
    if minutes:
        parts.append(f"{minutes} minute{'' if minutes == 1 else 's'}")
    if rest:
        parts.append(f"{rest} second{'' if rest == 1 else 's'}")
    return " ".join(parts)


# `.mergify.yml`'s queue rule key the policy's `timeouts.run` sets (mergify_rendered)
CHECKS_TIMEOUT = re.compile(r"^([ \t]*checks_timeout:[ \t]*)(.*?)[ \t]*$", re.MULTILINE)
MERGIFY_FILE = ".mergify.yml"


def mergify_drift(root: Path, loop: str | None, whole: bool = True) -> str | None:
    """Why the repo's `.mergify.yml` is out of step with the policy, None where it is in step or there is none: its
    `checks_timeout` other than `timeouts.run` gives (the one line `check setup` reads, `whole` False), else, with
    `whole`, any other difference from mergify_rendered."""
    path = root / MERGIFY_FILE
    if not path.exists():
        return None
    held, wanted = read(path), mergify_rendered(loop)
    found, want = CHECKS_TIMEOUT.search(held), CHECKS_TIMEOUT.search(wanted)
    have = found[2] if found else None
    if have != want[2]:  # type: ignore[index]
        return (
            f"{MERGIFY_FILE}'s `checks_timeout` is {have!r}, but the policy's `timeouts.run` makes it"
            f" {want[2]!r}: write `gatekeeper.py timeouts --mergify > {MERGIFY_FILE}`"  # type: ignore[index]
        )
    if whole and held != wanted:
        return (
            f"{MERGIFY_FILE} differs from this skill's mergify.yml beyond `checks_timeout`: copy it unchanged, with"
            f" `gatekeeper.py timeouts --mergify > {MERGIFY_FILE}`"
        )
    return None


def tree_policy(root: Path) -> str | None:
    """The working tree's own policy, chosen as project() chooses the base's: what this checkout's own config files
    (`.mergify.yml`, a rendered workflow) must agree with, so a PR that changes `timeouts` and the file together is in
    step."""
    found = [read(root / path) if (root / path).exists() else None for path in (LOOP, POLICY)]
    return chosen(*found)


WORKFLOWS = Path(".github/workflows")
# a job's `timeout-minutes` that follows `timeouts.run`, marked so the check knows which job is the selection's (#2445)
JOB_MINUTES = re.compile(
    r"^[ \t]*timeout-minutes:[ \t]*(\d+)[ \t]*#[ \t]*timeouts\.run\b", re.MULTILINE
)
# the workflow that is the project's nightly: one comment line says so, and its push paths must be `select.build_inputs`
NIGHTLY_MARK = re.compile(r"^#[ \t]*timeouts:[ \t]*nightly\b", re.MULTILINE)
# loop.md's line naming the variable a project's affected-tests wall-clock limit is set in, in seconds
WALL_CLOCK = re.compile(r"^Wall-clock variable:[ \t]*`?([A-Za-z_]\w*)", re.MULTILINE)


def push_paths(text: str) -> list[str]:
    """The `paths:` of a workflow's `push:` trigger, in order; empty when it has none (a push of any path)."""
    lines = text.splitlines()
    for at, line in enumerate(lines):
        if re.match(r"^[ \t]+push:[ \t]*$", line):
            indent = len(line) - len(line.lstrip())
            found: list[str] = []
            listing = False
            for row in lines[at + 1 :]:
                if row.strip() and len(row) - len(row.lstrip()) <= indent:
                    break
                if re.match(r"^[ \t]+paths:[ \t]*$", row):
                    listing = True
                elif listing and (item := re.match(r"^[ \t]+-[ \t]*(.+?)[ \t]*$", row)):
                    found.append(item.group(1).strip("\"'"))
                elif listing and row.strip() and not row.lstrip().startswith("#"):
                    listing = False
            return found
    return []


def limit_drift(root: Path, loop: str | None) -> list[str]:
    """One line for each limit out of step with `timeouts.run` (#2445), beside `.mergify.yml` (mergify_drift): a marked
    workflow job's `timeout-minutes`, the variable loop.md's `Wall-clock variable:` names (in seconds), a `land.wait`
    set by hand, and the nightly workflow's push paths against `select.build_inputs`. Each names the file and the
    value the budget gives."""
    out: list[str] = []
    budget, minutes = timeouts(loop)["run"], job_minutes(loop)
    flows = sorted((root / WORKFLOWS).glob("*.y*ml")) if (root / WORKFLOWS).is_dir() else []
    loop_file = root / LOOP
    named = WALL_CLOCK.search(read(loop_file)) if loop_file.exists() else None
    select = settings(loop).get("select", {})
    setting = re.compile(rf"\b{named[1]}\b[\"']?[ \t]*[:=][ \t]*[\"']?(\d+)") if named else None
    files = [(f"{WORKFLOWS}/{flow.name}", read(flow)) for flow in flows]
    if setting and (root / "mise.toml").exists():
        files.append(("mise.toml", read(root / "mise.toml")))
    for where, text in files:
        out += [
            f"{where}: `timeout-minutes` is {found[1]} but `timeouts.run` ({said_span(budget)}) makes it {minutes}"
            for found in JOB_MINUTES.finditer(text)
            if int(found[1]) != minutes
        ]
        if setting and (limit := setting.search(text)) and int(limit[1]) != budget:
            out.append(
                f"{where}: `{named[1]}` is {limit[1]} seconds but `timeouts.run` makes it {budget}"  # type: ignore[index]
            )
        if (
            NIGHTLY_MARK.search(text)
            and "build_inputs" in select
            and sorted(push_paths(text)) != sorted(select["build_inputs"])
        ):
            out.append(
                f"{where}: the nightly's push paths {push_paths(text)} differ from `select.build_inputs`"
                f" {list(select['build_inputs'])}"
            )
    land = settings(loop).get("land", {}).get("wait")
    if land is not None and (given := span(str(land))) and int(given.total_seconds()) != 2 * budget:
        out.append(
            f"{POLICY}: `land.wait` is {said_span(int(given.total_seconds()))} but `timeouts.run` makes it"
            f" {said_span(2 * budget)}: drop `land.wait`"
        )
    return out


def timeouts_verb(render: bool, check_only: bool, as_json: bool) -> int:
    """`timeouts`: every limit the working tree's policy derives from its `timeouts` block; `--mergify` prints the
    repo's `.mergify.yml` as rendered from it; `--check` exits 1 with one line where `.mergify.yml` is out of step."""
    root = toplevel()
    loop = tree_policy(root)
    if render:
        sys.stdout.write(mergify_rendered(loop))
        return 0
    if check_only:
        problems = [problem] if (problem := mergify_drift(root, loop)) else []
        problems += limit_drift(root, loop)
        if problems:
            print("\n".join(problems))
            return 1
        print("timeouts: every limit is in step with the policy's `timeouts`")
        return 0
    limits = timeouts(loop)
    derived = {
        "test": limits["test"],
        "run": limits["run"],
        "checks_timeout": said_span(limits["run"]),
        "job_timeout_minutes": job_minutes(loop),
        "land_wait": ceiling_of(loop),
    }
    if as_json:
        print(json.dumps(derived))
        return 0
    print(f"timeouts.test: {said_span(limits['test'])} (one test)")
    print(
        f"timeouts.run: {said_span(limits['run'])} (one selection run, the queue's check included)"
    )
    print(f"queue checks_timeout: {derived['checks_timeout']}")
    print(f"job timeout-minutes: {derived['job_timeout_minutes']}")
    overridden = "wait" in settings(loop).get("land", {})
    why = "land.wait, deprecated" if overridden else "the queue's checks timeout plus one more run"
    print(f"land --wait: {said_span(derived['land_wait'])} ({why})")
    return 0


def numbers_of(pr: int | Sequence[int] | None) -> list[int]:
    """The PR numbers a land names, each once."""
    return [] if pr is None else [pr] if isinstance(pr, int) else list(dict.fromkeys(pr))


def in_flight(number: int) -> bool:
    """Whether the last round left PR `number` to something that ends on its own, which is a land run again and no
    answer of the owner's: held by a merge queue (`watched["queued"]`), or waiting on its checks (a round that waited
    on neither the lock nor a queue, with the PR read: what Probe then watches). No GitHub read."""
    if number in watched["queued"]:
        return True
    return held_tip is None and any(p["number"] == number for p in watched["pulls"])


def wait_loop(pr: int | Sequence[int] | None, only: bool) -> int:
    """land's wait loop."""
    global held_tip
    # a land run again on the base's new code (caught_up) keeps the first run's ceiling; the clock is system-wide
    patience = land_wait()
    deadline = float(os.environ.pop(DEADLINE_ENV, "0")) or time.monotonic() + patience
    if earlier := json.loads(os.environ.pop(WORKTREES_ENV, "[]")):
        made.extend(earlier)  # execv runs no atexit: the next re-exec hands these on (caught_up)
        atexit.register(reap, earlier)
    said, spoke, heard = "", 0.0, ""
    # rounds in a row whose state did not change: the poll backs off with each (poll_seconds)
    quiet = 0
    github.wait_until = deadline  # a GitHub rate limit waits, inside the ceiling
    missed = 0  # network failures in a row, of landing() and of the lock poll
    while True:
        held_tip = None
        watched.update(
            base="", pulls=[], required=set(), deferred=False, queued={}
        )  # this round's, never an earlier one's
        out, drifted = io.StringIO(), None
        try:
            with contextlib.redirect_stdout(out):
                code, again = landing(pr, only)
            missed = 0
            # a round that took the lock and landed nothing (its PR is not landable yet) stamps `idle`: waiters behind
            # it take the free lock meanwhile, and it keeps its place for when its PR is ready
            waited["idle"], waited["took"] = waited["took"], False
        except Drifted as exc:
            drifted = exc
        except subprocess.CalledProcessError as exc:
            # one dropped connection is no reason to give up a wait that may have been hours long
            missed += 1
            if missed >= MISSES or not NETWORK.search(stderr_of(exc)):
                raise
            print(
                f"land --wait: network failure {missed}/{MISSES}, trying again: {stderr_of(exc)}",
                flush=True,
            )
            code, again = WAITING, True
        finally:
            # a background command's log: each state once, not every 30 seconds, a held lock's age being no new
            # state; again every SLOT_SAY_SECONDS, so wait-for's stall check never reads a long wait as a hang
            state = re.sub(r"\d+ minutes? ago", "", out.getvalue())
            quiet = quiet + 1 if state == said else 0
            if state != said or time.monotonic() - spoke >= SLOT_SAY_SECONDS:
                print(out.getvalue(), end="", flush=True)
                spoke = time.monotonic()
            said, heard = state, out.getvalue()
        if drifted:
            caught_up(drifted, deadline)
        if not again:
            if code:  # a wait that exits with its PR neither merged nor ended (main is red) must not leave `waiting`
                land_event_of(pr, "failed", unless_ended=True)
            return code
        nap = poll_seconds(quiet)
        if time.monotonic() + nap > deadline:
            said_for = said_span(patience)
            print(f"land --wait stopped after {said_for}, still waiting")
            # a PR a queue holds or whose checks run is being moved: it needs no owner, only a land run again
            moving = [n for n in numbers_of(pr) if in_flight(n)]
            land_event_of(moving, "waiting")  # land_event leaves a PR that asked the owner as it is
            land_event_of(
                [n for n in numbers_of(pr) if n not in moving],
                "needs-owner",
                f"land --wait stopped after {said_for} with the PR still waiting: land it again or"
                " say what holds it.\n",
            )
            return WAITING
        # the sleep goes in POLL steps, so the log is never silent past wait-for's stall check, and each step probes
        # only what the round waited on (Probe, #1915): a held lock that moved is seen at once, and one held by a live
        # land is never asked of GitHub; a CI state that moved runs the round again after `nap`, and nothing moving
        # runs it again only after FULL. A land that names no PR
        # and holds no lock has nothing to probe, so its round runs again after `nap`
        probe, asleep, beaten = Probe(), 0.0, 0.0
        while time.monotonic() + POLL <= deadline:
            time.sleep(POLL)
            github.read_afresh()  # the next read is GitHub's, not this run's memory (#2016)
            asleep += POLL
            if (
                heard and time.monotonic() - spoke >= SLOT_SAY_SECONDS
            ):  # wait-for's stall check reads silence as a hang
                print(heard, end="", flush=True)
                spoke = time.monotonic()
            if (asleep >= FULL and not probe.lock) or (
                (probe.idle or probe.stirred) and asleep >= nap
            ):
                break
            if probe.idle or probe.stirred:
                continue
            # the beats a round writes, which a probe alone would let go stale
            if asleep - beaten >= BEAT:
                beaten = asleep
                probe.beat()
            try:
                moved = probe.moved()
            except subprocess.CalledProcessError as exc:
                if not probe.lock:
                    # a CI or queue probe that failed is no news, never the wait's end: the round, after its poll,
                    # reads all again and has its own network retries
                    print(
                        f"land --wait: the {'queue' if probe.queued else 'CI'} probe failed, the round reads again:"
                        f" {stderr_of(exc)}",
                        flush=True,
                    )
                    probe.stirred = True
                    continue
                missed += 1
                if missed >= MISSES or not NETWORK.search(stderr_of(exc)):
                    raise
                print(
                    f"land --wait: network failure {missed}/{MISSES}, polling again: {stderr_of(exc)}",
                    flush=True,
                )
                continue
            missed = 0
            if moved:
                break


class Probe:
    """What a waiting land reads between two landing() rounds (FULL says what): the held lock the last round waited
    on, else the base, the base's lock and the named PRs' heads and their checks. `idle` when it has neither,
    `stirred` once a CI state moved (the round then runs again after its poll, with nothing more read meanwhile). Each
    starts from what the round read (the base's tip and each head as the PRs gave them, each head's checks read at
    once), so a move between the round and the first poll is still a move."""

    def __init__(self) -> None:
        self.lock = held_tip
        self.since = time.monotonic()  # when the held lock last moved
        self.base, self.required = watched["base"], set(watched["required"])
        self.pulls = list(watched["pulls"])
        # each named PR's head ref, and the head the round read, which a push moves
        self.heads = {
            f"refs/pull/{p['number']}/head": p.get("headRefOid") or "" for p in self.pulls
        }
        self.base_tip: str | None = next(
            (p["baseRefOid"] for p in self.pulls if p.get("baseRefOid")), None
        )
        self.lock_tip: str | None = None
        self.deferred = bool(watched["deferred"])
        self.checks: dict[str, tuple[tuple[str, str], ...]] = {}
        # the named PRs a merge queue holds, and when their queue state was last read (the round, just now)
        self.queued: dict[int, str] = dict(watched.get("queued") or {})
        self.watched_at = time.monotonic()
        self.idle = not self.lock and not self.heads
        self.stirred = False
        if not self.lock and not self.queued:
            for head in filter(None, self.heads.values()):
                try:
                    self.checks[head] = ci_state(head, self.required)
                except subprocess.CalledProcessError:
                    pass  # the first poll reads it

    def beat(self) -> None:
        """This land's place in the waiting order and each named PR's `waiting` event, as a round writes them, so a
        quiet wait never reads as a dead one: both are git pushes, no GitHub API call."""
        if waited["id"] and waited["base"]:
            try:
                stamp_wait(waited["base"], self.pulls[0]["number"] if self.pulls else None)
            except subprocess.CalledProcessError as exc:
                print(
                    f"land --wait: its place in the waiting order was not stamped: {stderr_of(exc)}",
                    file=sys.stderr,
                )
        for pull in self.pulls:
            if pull["headRefName"] not in ended:
                land_event(pull, "waiting")

    def moved(self) -> bool:
        """Whether landing() should run again now: the held lock moved past a heartbeat, or has not moved for STALE
        seconds; a PR a merge queue holds merged, closed, left the queue or was pushed to (queue_moved); for a wait on
        CI, never: it sets `stirred` instead, which runs the round after its poll."""
        if self.lock:
            return self.lock_moved()
        if self.queued:
            return self.queue_moved()
        self.stirred = self.ci_moved()
        return False

    def queue_moved(self) -> bool:
        """At most once per QUEUE_WATCH seconds, one queue_watch read of the PRs a merge queue holds (#2101): True once
        one is no longer open (merged, closed), is no longer in GitHub's queue, carries Mergify's DEQUEUED or lost
        QUEUED, or has a head other than the round's (a push, which takes it out of either queue). A red check takes a
        PR out of the queue, so it is seen as a removal; the round then reads what happened."""
        if time.monotonic() - self.watched_at < QUEUE_WATCH:
            return False
        self.watched_at = time.monotonic()
        found = queue_watch(sorted(self.queued))
        for number, which in self.queued.items():
            now, head = found[number], self.heads.get(f"refs/pull/{number}/head")
            if now["state"] != "OPEN" or (head and now["headRefOid"] != head):
                return True
            if which == GITHUB and not now["isInMergeQueue"]:
                return True
            if which == MERGIFY and (DEQUEUED in now["labels"] or QUEUED not in now["labels"]):
                return True
        return False

    def lock_moved(self) -> bool:
        assert self.lock
        ref, tip = self.lock
        found = git("ls-remote", "origin", ref).split()[:1]
        if found == [tip]:
            return time.monotonic() - self.since >= STALE
        if found and restamped(ref, found[0], tip):
            global held_tip
            self.lock = held_tip = (ref, found[0])
            self.since = time.monotonic()
            return False
        return True

    def ci_moved(self) -> bool:
        branch, lock = f"refs/heads/{self.base}", LOCK.format(self.base)
        listed: dict[str, str] = {}
        watch = [branch, *([lock] if self.deferred else []), *self.heads]
        for line in git("ls-remote", "origin", *watch).splitlines():
            sha, _, name = line.partition("\t")
            listed[name.strip()] = sha.strip()
        base = listed.get(branch)
        if self.base_tip is None:
            self.base_tip = base
        elif base != self.base_tip:
            return True
        # the base's lock freed, where the round left it to another land: that land is done with it, so this one may
        # take it
        tip = listed.get(lock)
        if tip and self.lock_tip is not None and tip != self.lock_tip and lock_free(lock, tip):
            return True
        self.lock_tip = tip or ""
        for ref, head in list(self.heads.items()):
            now = listed.get(ref) or head
            if head and now != head:
                return True
            self.heads[ref] = now
            if not now:
                continue
            state = ci_state(now, self.required)
            if self.checks.setdefault(now, state) != state:
                return True
        return False


def restamped(ref: str, tip: str, old: str) -> bool:
    """Whether the lock's new `tip` is a HELD child of `old`: its holder's heartbeat, or a land that broke it as stale
    and holds it now, so a waiting land still waits."""
    name = ref.removeprefix("refs/heads/")
    git("fetch", "-q", "origin", f"+{ref}:refs/remotes/origin/{name}")
    title, _, parents = git("log", "-1", "--format=%s%x00%P", tip).strip().partition("\0")
    return title.startswith(HELD) and parents.split()[:1] == [old]


def lock_free(ref: str, tip: str) -> bool:
    """Whether the lock's `tip` says it is free (FREE), fetched first: git only, no API call."""
    name = ref.removeprefix("refs/heads/")
    git("fetch", "-q", "origin", f"+{ref}:refs/remotes/origin/{name}")
    return git("log", "-1", "--format=%s", tip).strip().startswith(FREE)


def ci_state(head: str, required: set[str]) -> tuple[tuple[str, str], ...]:
    """The state of each check on `head` a wait on CI reads: the base's `required` ones, or every check but the
    loop's own (OWN) where it requires none, each `pending` until it completes, from two ETag reads (rest_if_changed),
    which cost no quota while nothing changed. A check run still running moves no required check's state, so it
    runs no round."""
    _, runs = github.rest_if_changed(f"commits/{head}/check-runs?filter=latest&per_page=100")
    _, combined = github.rest_if_changed(f"commits/{head}/status?per_page=100")
    states: dict[str, str] = {}
    for status in combined.get("statuses") or []:  # newest first
        states.setdefault(status["context"], status["state"])
    for run in runs.get("check_runs") or []:
        done = run.get("status") == "completed"
        states[run["name"]] = (run.get("conclusion") or "pending") if done else "pending"
    names = required or set(states) - OWN
    return tuple(sorted((name, states.get(name, "missing")) for name in names))


DEADLINE_ENV = "LOOP_LAND_DEADLINE"
# the worktrees this process's ancestors made (land), handed on by caught_up
made: list[list[str]] = []
WORKTREES_ENV = (
    "LOOP_LAND_WORKTREES"  # JSON [[git dir, path]] of the worktrees caught_up made, removed at exit
)


def caught_up(drifted: Drifted, deadline: float) -> NoReturn:
    """A waiting land whose base moved its code or policy (another land merged a change to them) runs again from the
    base's new code, keeping its ceiling, since the wait was the point and nobody reads its log (#1280, #1314). A clean
    checkout of the base is fast-forwarded to the fetched `origin/<base>` and the land runs again in it (#1280). Any
    other checkout (a linked worktree on a PR branch or a detached head, an edited tracked file, a fast-forward
    refused) is left as it is, and the land runs again from a fresh detached worktree of `origin/<base>`, removed when
    that run ends (#1314), since moving a checkout the caller is using is not ours to do and a PR's own copy must never
    judge it. Nothing newer to run from (this checkout already is the base's tip, or the base lacks the running script)
    keeps the refusal, which would otherwise run again forever."""
    base, root = drifted.base, toplevel()
    ref = f"refs/remotes/origin/{base}"

    def run(where: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(where), *args], capture_output=True, text=True, check=False
        )

    def out(where: Path, *args: str) -> str:
        return run(where, *args).stdout.strip()

    before, tip = out(root, "rev-parse", "HEAD"), out(root, "rev-parse", ref)
    if before == tip and not out(root, "status", "--porcelain", "--untracked-files=no"):
        raise drifted
    script = Path(sys.argv[0]).resolve()
    argv, cwd = [sys.executable, *sys.argv], None
    if (
        out(root, "symbolic-ref", "-q", "HEAD") == f"refs/heads/{base}"
        and not out(root, "status", "--porcelain", "--untracked-files=no")
        and not run(root, "merge", "-q", "--ff-only", ref).returncode
        and out(root, "rev-parse", "HEAD") != before
    ):
        where = f"this checkout fast-forwarded to origin/{base}"
    else:
        fresh = Path(tempfile.mkdtemp(prefix=f"land-{base.replace('/', '-')}-")).resolve()
        added = run(root, "worktree", "add", "-q", "--detach", str(fresh), tip)
        if added.returncode:
            fresh.rmdir()
            raise drifted
        gitdir = Path(out(root, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        os.environ[WORKTREES_ENV] = json.dumps([*made, [str(gitdir), str(fresh)]])
        # the running script as the base has it; one outside the checkout is not drift, so it stays as it is
        if script.is_relative_to(root.resolve()):
            argv[1] = str(fresh / script.relative_to(root.resolve()))
            if not Path(argv[1]).exists():  # the base lacks it: nothing to run again from
                reap([[str(gitdir), str(fresh)]])
                raise drifted
        cwd, where = fresh, f"a fresh worktree of origin/{base} ({fresh})"
    print(
        f"{drifted} -- {where} ({tip[:12]}): land runs again from its code",
        flush=True,
    )
    os.environ[DEADLINE_ENV] = repr(deadline)
    if waited["id"]:
        os.environ[WAIT_ENV] = ":".join(
            [repr(waited["since"]), waited["id"], waited["base"], waited["tip"]]
        )
    if cwd:
        os.chdir(cwd)
    os.execv(sys.executable, argv)


def reap(worktrees: Sequence[Sequence[str]]) -> None:
    """Remove the fresh worktrees that caught_up made for a land run again, now that none of them is this process's cwd."""
    os.chdir(tempfile.gettempdir())
    for gitdir, path in worktrees:
        subprocess.run(
            ["git", f"--git-dir={gitdir}", "worktree", "remove", "--force", path],
            capture_output=True,
            check=False,
        )


def poll_seconds(quiet: int = 0) -> float:
    """QUEUE_POLL, doubled for each of the `quiet` rounds in a row whose state did not change up to QUIET_POLL (#1771),
    or LOW_POLL when that is longer and the REST quota left is under LOW_QUOTA, plus up to POLL_JITTER random seconds
    so waiting lands do not poll in the same minute. The quota is the guard's reading (github.quota), from `gh api
    rate_limit`, which is not counted against it; gh shows no response headers, so nothing is read for free. A failed
    read keeps the quiet poll."""
    first = max(POLL, QUEUE_POLL)
    base = min(first * 2 ** min(quiet, 8), max(QUIET_POLL, first))
    found = github.quota("core")
    if found and found[0] < LOW_QUOTA * found[1]:
        base = max(base, LOW_POLL)
    return base + github.random.randint(0, POLL_JITTER)


def landing(pr: int | Sequence[int] | None, only: bool = False) -> tuple[int, bool]:
    """Land every landable PR into the base (the first named PR's, else the default branch) through its merge queue,
    the GitHub merge queue where its rules require one, else the local merge queue, where loop.md lets land run (placed)
    (`only`: the named PRs alone); the exit, and whether a wait would end it (lock, CI on a head, a queue entry), so
    land --wait runs it again. Every decision is the policy of the base the PRs
    merge onto (policy_from): the named PRs' base is read first (land_base, which changes no open PR, and gives one the GitHub merge queue merged its
    `Landed` comment: queue_landed), the base fetched,
    and its policy read to place the land (placed: where it may run and the key it signs with) and to ready drafts,
    then read again after the fetch under the lock for the batch (landing_here)."""
    ended.clear()
    watched.update(base="", pulls=[], required=set(), deferred=False, queued={})
    named, base, worst = land_base(pr)
    if named is None:
        return worst, False
    watched.update(base=base, pulls=list(named))
    own_prs(named)
    # origin's policy now, not the clone's last fetch of it, so no draft is readied under a policy already replaced
    fetch = subprocess.run(
        ["git", "fetch", "-q", "origin", f"+refs/heads/{base}:refs/remotes/origin/{base}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if fetch.returncode:
        print(
            f"land could not fetch {base} from origin, so it readies no draft before the lock:"
            f" {' '.join(fetch.stderr.split()) or f'exit {fetch.returncode}'}"
        )
    with policy_from(base):
        tracker, loop = project()
        # the base's rules pick the merge queue, and with it whether a cloud land signs (placed)
        rules = required_checks(base)
        watched["required"] = set(rules) - OWN
        # Mergify, like GitHub's queue, merges and signs each PR itself, so a cloud land needs no key for it
        with placed(loop, held_by(loop, rules) != "land"):
            code, again = landing_here(
                named, base, worst, tracker, loop, only, not fetch.returncode, rules
            )
        if (
            waiting and again
        ):  # the beat of a wait that goes on: each poll rewrites it, but never an end of this round
            for pull in named:
                if pull["headRefName"] not in ended:
                    land_event(pull, "waiting")
        return code, again


def land_base(
    pr: int | Sequence[int] | None,
) -> tuple[list[dict[str, Any]] | None, str, int]:
    """The named PRs that can land together, the base they merge onto (the first one's, else the default branch) and
    the worst exit of those left out (1 or 0); None for the PRs when every named one was left out."""
    numbers = [] if pr is None else [pr] if isinstance(pr, int) else list(dict.fromkeys(pr))
    named: list[dict[str, Any]] = []
    worst = 0  # of the named PRs that end here, 1 over 3 over 0
    for number in numbers:
        pull = pull_request(number)
        if pull["state"] == "MERGED":
            print(f"PR #{number} is merged")
            queue_landed(pull)
        elif pull["state"] != "OPEN":
            if len(numbers) == 1:
                raise Refused(f"PR #{number} is closed: nothing to land")
            print(f"PR #{number} is closed: nothing to land")
            worst = 1
        elif named and pull["baseRefName"] != named[0]["baseRefName"]:
            print(
                f"PR #{number} targets {pull['baseRefName']}, not {named[0]['baseRefName']} like #{named[0]['number']}:"
                " land one base at a time"
            )
            worst = 1
        else:
            named.append(pull)
    if numbers and not named:
        return None, "", worst
    base = named[0]["baseRefName"] if named else default_branch()
    return named, base, worst


def landing_here(
    named: list[dict[str, Any]],
    base: str,
    worst: int,
    tracker: str | None,
    loop: str | None,
    only: bool = False,
    ready: bool = True,
    rules: set[str] | None = None,
) -> tuple[int, bool]:
    """land_base's batch under the lock, `rules` the base's required checks (required_checks, read here when None). `tracker` and `loop`, the policy read before the lock, decide only which
    drafts to mark ready before the wait (none without `ready`: the base's fetch failed, so its policy may be stale),
    so the holder's next batch includes them; after the fetch under the lock the policy is read again, and that copy
    alone decides the batch: the land is placed again under it (placed: refused, the lock released, where it now
    excludes this place, and signed with its key), every PR is judged under it, a draft this land readied that it would
    not have readied goes back to draft (unreadied), and a verdict readied() kept for a draft it refused is reused only
    when the policy is unchanged."""
    # the base's rules and the policy pick the merge queue (held_by), never a mix of two;
    # `loop:approvals`, `gatekeeper` and `land` are the loop's own statuses (OWN), on the proofs land reads itself
    rules = required_checks(base) if rules is None else rules
    required = rules - OWN
    queue = held_by(loop, rules)  # before readied, so a refused policy touches no PR
    # a draft is ready before the wait for the lock, so the holder's next batch includes it
    if ready:
        named = readied(named, required, tracker, loop)
    if queue == GITHUB:
        return github_queue(named, base, tracker, loop, required, rules.queue, only, worst)
    if queue == MERGIFY:
        return mergify_queue(named, base, tracker, loop, required, only, worst)
    previous = {sig: signal.getsignal(sig) for sig in STOPS}
    stoppable = {sig for sig, handler in previous.items() if handler is not signal.SIG_IGN}
    # every lock commit pushed, each before its push; taken stays None while the last one's push may have reached origin
    held: list[str] = []
    taken = None
    stop = threading.Event()
    try:
        for sig in stoppable:  # a land under nohup still outlives a hangup
            signal.signal(sig, stopped)
        taken = locked(base, held, named[0]["number"] if named else None)
        if not taken:
            # a named PR left out already is a refusal, which the wait for the lock doesn't clear: 1 over 3
            return (1 if worst else WAITING), True
        beating_for[:] = named
        threading.Thread(target=heartbeat, args=(base, held, stop), daemon=True).start()
        # the base as origin has it once the lock is ours, so no land moved it between this fetch and the batch
        git("fetch", "-q", "origin", f"+refs/heads/{base}:refs/remotes/origin/{base}")
        judge_with_base(base)
        # another land may have changed the base's policy while this one waited: from here on only the fetched one
        fresh = project()
        if fresh != (tracker, loop):
            named = unreadied(
                [{k: v for k, v in pull.items() if k != "verdict"} for pull in named], base, *fresh
            )
        with placed(fresh[1]):
            return local_queue(named, base, *fresh, required, only, held, worst)
    finally:
        # a stop from here on waits, blocked, until the lock is released and the handlers are back: raised inside the
        # release it would kill its push and leave the lock. The push runs in a session of its own, since some of its
        # children (ssh, gh as the credential helper) unblock the signals, and a stop of the land's group would end them
        mask = signal.pthread_sigmask(signal.SIG_BLOCK, STOPS)
        stop.set()
        beating_for.clear()
        with (
            beating
        ):  # the beat in flight finishes, so `held` ends on the commit that reached origin
            pass
        if held and taken is not False:
            released = unlock(base, held[-1])
            if released.returncode and taken:
                print(
                    f"the landing lock {lock_name(base)} was not released: {' '.join(released.stderr.split())}"
                )
        for sig, handler in previous.items():
            if handler is not None:  # None: a handler set outside Python, which can't be put back
                signal.signal(sig, handler)
        waiting = signal.sigpending() & stoppable
        # taken here, so a stop that waited ends the land as one caught earlier would, not by the default handler
        for sig in waiting:
            signal.sigwait({sig})
        signal.pthread_sigmask(signal.SIG_SETMASK, mask)
        if waiting:
            sys.exit(128 + min(waiting))


# the script files running here: this one, and the gatekeeper beside it when this is the approvals.py shim that runs
# the gatekeeper's source (the same file, once, when the gatekeeper runs directly)
RUNNING = tuple(dict.fromkeys((__file__, str(Path(__file__).with_name("gatekeeper.py")))))


def base_drift(base: str, rules: bool = True) -> list[str]:
    """The running gatekeeper.py and github_adapter.py and the three policy files (tracker, loop.md, gatekeeper.md) whose text here differs from
    `origin/<base>`'s, as repo paths, since a PR's own edit of the code or rules that judge it must not judge it. Reads
    the ref as fetched (a land has just fetched it); a script inside the checkout that the base lacks (a PR adds or moves it) differs; only one
    installed outside the checkout, or a ref that is missing, has nothing to compare and counts as equal. A policy file
    is compared as this run reads it (policy_bytes): with no working tree, origin's default branch's, so a land into
    the default branch finds no drift by construction, and one into another base still compares with that base's.
    Without `rules`, the code alone: for a caller that reads every policy file from the base (project()) and writes
    nothing, as `check stalled`, a working tree's edit of them judges nothing."""
    root = toplevel()
    ref = f"refs/remotes/origin/{base}"
    if subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--verify", "-q", ref],
        capture_output=True,
        check=False,
    ).returncode:
        return []
    paths: list[Path] = []
    # the gatekeeper and its GitHub adapter, the code that judges
    for running_file in reversed(dict.fromkeys((*RUNNING, github.__file__ or ""))):
        try:
            paths.insert(0, Path(running_file).resolve().relative_to(root.resolve()))
        except ValueError:
            pass
    differ = []
    for path in [*paths, *((TRACKER, LOOP, POLICY) if rules else ())]:
        theirs = shown_at(root, ref, path)
        if path in (TRACKER, LOOP, POLICY):
            ours = policy_bytes(root, path)
        else:
            try:
                ours = (root / path).read_bytes()
            except OSError:
                ours = None
        if ours != theirs:
            differ.append(path.as_posix())
    return differ


def judge_with_base(base: str, fetch: bool = False, rules: bool = True) -> None:
    """Refuse, naming the files, when the code or policy running here is not `origin/<base>`'s, so the caller runs from
    the base's code; under GITHUB_ACTIONS (a workflow that checks out the PR) also when HEAD is not the base's tip.
    Without `rules`, only the code is compared (base_drift)."""
    if fetch:  # a land has fetched it already
        fetched_base = subprocess.run(
            [
                "git",
                "-C",
                str(toplevel()),
                # through `gh`, so GH_TOKEN works where the checkout persisted no credentials
                "-c",
                "credential.helper=",
                "-c",
                "credential.helper=!gh auth git-credential",
                "fetch",
                "-q",
                "origin",
                f"+refs/heads/{base}:refs/remotes/origin/{base}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if fetched_base.returncode:
            raise Refused(
                f"could not fetch origin/{base} ({' '.join(fetched_base.stderr.split()) or 'no reason given'}):"
                " a stale ref would judge by another tip's files"
            )
    if os.environ.get("GITHUB_ACTIONS") == "true":
        tip = subprocess.run(
            [
                "git",
                "-C",
                str(toplevel()),
                "rev-parse",
                "--verify",
                "-q",
                f"refs/remotes/origin/{base}^{{commit}}",
            ],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        if tip and tip != git("rev-parse", "HEAD").strip():
            raise Refused(
                f"the checked-out commit is not origin/{base}'s tip ({tip[:12]}): a workflow that checks out the PR judges"
                " it by its own rules; check out the base"
            )
    if differ := base_drift(base, rules):
        raise Drifted(
            f"{', '.join(differ)} differ{'s' * (len(differ) == 1)} from origin/{base}'s: a PR's own copy would judge it;"
            f" run from the base's code (a checkout of origin/{base})",
            base,
        )


def stopped(signum: int, _frame: Any) -> None:
    """A land stopped by SIGTERM or SIGHUP (a killed process group, a harness timeout) unwinds, so landing's finally
    releases the lock. Further stops are ignored first: `uv run` forwards the same signal again, and a second exit
    raised on the way to the release would cut landing's cleanup short. SIGKILL can't be caught, and leaves the lock to
    be broken as stale."""
    for sig in STOPS:
        signal.signal(sig, signal.SIG_IGN)
    sys.exit(128 + signum)


def wait_push(title: str, base: str, body: str) -> None:
    """Push a `title` child of this land's last waiting commit to its waiting ref: a fast-forward (the first one
    creates the branch), never a force or a delete, so a host that may only create and fast-forward branches (a cloud
    session) takes part. A failed push (the network) leaves the last stamp in place, which stays live for
    WAITER_STALE; one whose answer was lost is found again on origin."""
    ref = WAIT_REF.format(base, waited["id"])
    empty = git("hash-object", "-t", "tree", "-w", os.devnull).strip()
    commit = git(
        "commit-tree",
        *(["-p", waited["tip"]] if waited["tip"] else []),
        "-m",
        f"{title} [skip ci]\n\n{body}, pid {os.getpid()}, nonce {os.urandom(4).hex()}",
        empty,
    ).strip()
    pushed = subprocess.run(
        ["git", "push", "-q", "--no-verify", "origin", f"{commit}:{ref}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if not pushed.returncode or git("ls-remote", "origin", ref).split()[:1] == [commit]:
        waited["tip"] = commit


def stamp_wait(base: str, pr: int | None) -> None:
    """Stamp this waiting land's place: a commit dated now, its body holding when the land began to wait (to the
    millisecond, as `waiter_ahead` compares it, so two lands never read each other's order differently)."""
    waited["since"] = waited["since"] or round(time.time(), 3)
    waited["id"] = waited["id"] or os.urandom(6).hex()
    waited["base"] = base
    wait_push(
        "waiting",
        base,
        f"since {waited['since']:.3f}"
        + (f", PR #{pr}" if pr is not None else "")
        + (", idle" if waited["idle"] else ""),
    )


def waiter_ahead(base: str) -> str | None:
    """The PR text of the longest live waiter that began before this land (None: none does, or origin can't be read).
    A ref marked done, or whose land has not stamped for CEILING (it died), is dropped where origin lets us delete."""
    mine = (waited["since"], waited["id"])
    ahead = None
    try:
        refs = [
            x.split("\t")[1]
            for x in git("ls-remote", "origin", WAIT_REF.format(base, "*")).splitlines()
        ]
        if refs:
            git(
                "fetch",
                "-q",
                "origin",
                *(f"+{r}:{r.replace('refs/heads/', 'refs/remotes/origin/')}" for r in refs),
            )
        for ref in refs:
            stamp, title, body = git(
                "log",
                "-1",
                "--format=%ct%x00%s%x00%b",
                ref.replace("refs/heads/", "refs/remotes/origin/"),
            ).split("\0")
            age = time.time() - int(stamp)
            if title.startswith("done") or age > CEILING:
                subprocess.run(
                    ["git", "push", "-q", "--no-verify", "origin", "--delete", ref],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                continue
            since = re.search(r"since (\d+(?:\.\d+)?)", body)
            theirs = (float(since[1]) if since else 0.0, ref.rsplit("/", 1)[1])
            if (
                theirs[1] != waited["id"]
                and age <= WAITER_STALE
                and ", idle" not in body
                and theirs < mine
                and (ahead is None or theirs < ahead[0])
            ):
                named = re.search(r"PR #(\d+)", body)
                ahead = (
                    theirs,
                    f"PR #{named[1]}" if named else f"pid {body.split('pid ')[-1].split(',')[0]}",
                )
    except subprocess.CalledProcessError:
        return None
    return ahead[1] if ahead else None


def unwait(forget: bool = False) -> None:
    """This land stops counting as a waiter: a done child of its ref (a fast-forward, which every host may push), then a
    delete where the host may. `forget`: the wait is over, so its place goes too; otherwise (the land took the lock)
    the place is kept, so a land that takes it and lands nothing does not rejoin at the back."""
    if waited["id"] and waited["tip"]:
        wait_push("done", waited["base"], f"since {waited['since']:.3f}")
        subprocess.run(
            [
                "git",
                "push",
                "-q",
                "--no-verify",
                "origin",
                "--delete",
                WAIT_REF.format(waited["base"], waited["id"]),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    if forget:
        waited.update(since=0.0, id="", base="", tip="", took=False, idle=False)


def lock_name(base: str) -> str:
    """The landing lock's branch for `base`, as messages name it."""
    return LOCK.format(base).removeprefix("refs/heads/")


def lock_commit(title: str, parent: str | None, pr: int | None = None) -> str:
    """A landing lock commit: the empty tree, `title` (HELD or FREE) with [skip ci], `parent` the tip it moves on from,
    and in its body the pid and the PR `pr` it lands last (so a waiting land reads the lock as released once that PR
    merged: released; no hostname goes on the public branch), then a random value, since two lands in the same
    second, under one identity, would otherwise build one commit, and the second push of it would succeed as a no-op.
    Its commit date is its age."""
    empty = git("hash-object", "-t", "tree", "-w", os.devnull).strip()
    landing = f", PR #{pr}" if pr is not None else ""
    return git(
        "commit-tree",
        *(["-p", parent] if parent else []),
        "-m",
        f"{title} [skip ci]\n\npid {os.getpid()}{landing}, {BEATS}, nonce {os.urandom(8).hex()}",
        empty,
    ).strip()


def leased(base: str, ours: str, tip: str | None, **kw: Any) -> subprocess.CompletedProcess[str]:
    """Push `ours`, a child of `tip` (None: the branch is missing), to the base's lock branch, leased on `tip`, so of
    two lands pushing from one tip one wins and nothing is ever rewritten. --no-verify, since a lock commit holds the
    empty tree and a project's pre-push checks have nothing of it to check."""
    ref = LOCK.format(base)
    return subprocess.run(
        [
            "git",
            "push",
            "-q",
            "--no-verify",
            f"--force-with-lease={ref}:{tip or ''}",
            "origin",
            f"{ours}:{ref}",
        ],
        capture_output=True,
        text=True,
        check=False,
        **kw,
    )


def heartbeat(base: str, held: list[str], stop: threading.Event) -> None:
    """While the lock is ours, every BEAT seconds push a HELD child of our newest commit, leased on it, so the lock's
    stamp stays fresh. A refused push means another land took the lock (broke it as stale, or read its PR as merged):
    our commits are dropped from `held`, which nothing then releases, as repointed() does. A push
    that fails for another reason (the network) is tried again at the next beat. A stop signal is left to the main
    thread, which releases the lock."""
    signal.pthread_sigmask(signal.SIG_BLOCK, STOPS)

    # Only a `land --wait` beats `waiting` (events.md): a land that is not waiting ends with no event of its own, and a
    # stale `waiting` would read as a live wait until it went silent. The first beat comes when the lock is taken, since
    # a gate under the lock can outlast a native wait's two beats of silence; the lock's own stamp is fresh from the
    # take, so its first push waits a BEAT.
    def beat() -> None:
        for pull in beating_for if waiting else []:
            if not stop.is_set():
                land_event(pull, "waiting")

    beat()
    while not stop.wait(BEAT):
        beat()
        with beating:
            if stop.is_set() or not held:
                return
            ours = held[-1]
            # the PR our newest commit names, so a land waiting on this lock still reads it as released once that merges
            try:
                named = re.search(r"\bPR #(\d+)\b", git("log", "-1", "--format=%b", ours))
                held.append(lock_commit(HELD, ours, int(named[1]) if named else None))
            except (
                subprocess.CalledProcessError
            ):  # git failed locally: no beat this time, the next one tries again
                continue
            try:
                pushed = leased(
                    base, held[-1], ours, timeout=BEAT_PUSH
                )  # a hung push must not hold `beating` for ever
            except subprocess.TimeoutExpired:
                pushed = subprocess.CompletedProcess([], 1)
            if pushed.returncode:
                tip = lock_tip(base)
                if (
                    tip is None
                ):  # unreadable (the network): the commit may have reached origin, so it stays ours
                    continue
                if (
                    tip in held
                ):  # a push reached origin and only its answer was lost: what came after it did not
                    del held[held.index(tip) + 1 :]
                    continue
                held.clear()  # another land's tip
                return


def lock_tip(base: str) -> str | None:
    """The lock branch's tip on origin, None where it is missing or unreadable."""
    try:
        found = git("ls-remote", "origin", LOCK.format(base)).split()
    except subprocess.CalledProcessError:
        return None
    return found[0] if found else None


def unlock(base: str, ours: str) -> subprocess.CompletedProcess[str]:
    """Release the landing lock: push a FREE child of `ours`, leased on it, so a lock broken as stale and taken by
    another land meanwhile stays theirs. In a session of its own (landing says why)."""
    return leased(base, lock_commit(FREE, ours), ours, start_new_session=True)


def locked(base: str, held: list[str], pr: int | None = None) -> bool:
    """Take the base's landing lock, naming `pr`: push a HELD child of its FREE tip (the first lock commit, with no
    parent, where the branch is missing), leased on that tip. A HELD tip is another land's; one older than CEILING by
    its commit date is stale, and one naming a PR already merged into the base is released (released): either is
    broken and taken by the same push of a HELD child. Each commit is put in `held` before its push, so a stop
    as it reaches origin still releases it. False, after saying who holds it since when, while another land holds it
    or LEGACY, the previous version's lock, is on origin and no older than CEILING."""
    ref, name = LOCK.format(base), lock_name(base)
    if waiting:
        stamp_wait(base, pr)
    tip, refused = (
        "",
        "",
    )  # "": not read yet; a refusal of a push from the tip read again is origin's, not a race's
    for _ in range(3):
        listed = {
            line.split("\t")[1] for line in git("ls-remote", "origin", LEGACY, ref).splitlines()
        }
        if LEGACY in listed:
            try:
                git("fetch", "-q", "origin", f"+{LEGACY}:{LEGACY}")
            except subprocess.CalledProcessError as exc:
                if git(
                    "ls-remote", "origin", LEGACY
                ).strip():  # still there: the fetch itself failed
                    raise Refused(
                        f"could not read the previous version's landing lock {LEGACY}: {' '.join(str(exc.stderr).split())}"
                    ) from exc
                continue  # released since the listing: read origin again
            age = int(time.time()) - int(git("log", "-1", "--format=%ct", LEGACY).strip())
            if age <= CEILING:
                print(
                    f"a land of the previous version holds the landing lock {LEGACY}: land --wait waits for it"
                )
                return False
            # stale, left by a land that died: passed over, never written, as the previous version would break it
        before, tip = tip, None
        if ref in listed:
            git("fetch", "-q", "origin", f"+{ref}:refs/remotes/origin/{name}")
            tip = git("rev-parse", f"refs/remotes/origin/{name}").strip()
        if refused and tip == before:
            raise Refused(f"origin refused the landing lock {name}: {refused}")
        if tip:
            stamp, title, holder = git("log", "-1", "--format=%ct%x00%s%x00%b", tip).split("\0")
            if title.startswith(HELD):
                taken = int(stamp)
                age = int(time.time()) - taken
                holder = holder.strip().split(", nonce ")[0]
                dead = age > STALE and f", {BEATS}" in holder
                if age <= CEILING and not dead and (done := released(base, holder)):
                    print(f"the landing lock {name} ({holder}) is released: {done}: broken")
                elif dead and age <= CEILING:
                    print(
                        f"the landing lock {name} ({holder}) was last stamped {age // 60} minutes ago, past"
                        f" {STALE // 60}: its land is gone, broken as stale"
                    )
                elif age <= CEILING:
                    global held_tip
                    held_tip = (ref, tip)
                    print(
                        f"another land holds the landing lock {name} ({holder}), taken {age // 60} minutes ago"
                        f" ({time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(taken))}): land --wait waits for it"
                    )
                    return False
                else:
                    print(
                        f"the landing lock {name} is {age // 3600} hours old, past {CEILING // 3600} hours: broken as"
                        " stale"
                    )
        if waiting and (first := waiter_ahead(base)):
            watched["deferred"] = True
            print(
                f"{first} began waiting for the landing lock {name} before this land, so it takes it first"
            )
            return False
        held.append(lock_commit(HELD, tip, pr))
        pushed = leased(base, held[-1], tip)
        if not pushed.returncode:
            unwait()
            waited["took"] = True
            return True
        refused = " ".join(pushed.stderr.split())
    print(
        f"other lands kept taking the landing lock {name} as this one tried to: land --wait waits for it"
    )
    watched["deferred"] = True
    return False


def released(base: str, holder: str) -> str | None:
    """Why a HELD lock whose body is `holder` is released though never freed: the PR it names (the last its land
    merges) is merged and origin's base holds its merge commit, so its land's work is done; None for a lock naming no
    PR (written before #901, waited on until CEILING), an open one, a merge the base lacks, or a PR GitHub can't read
    now (waited on, so a GitHub outage breaks no live land's lock)."""
    if (named := re.search(r"\bPR #(\d+)\b", holder)) is None:
        return None
    number = int(named[1])
    try:
        pull = merged_pull(number)
    except subprocess.CalledProcessError:
        return None
    if pull is None or not (sha := pull.get("merge_commit_sha")):
        return None
    subprocess.run(
        ["git", "fetch", "-q", "origin", f"+refs/heads/{base}:refs/remotes/origin/{base}"],
        capture_output=True,
        check=False,
    )
    contained = subprocess.run(
        ["git", "merge-base", "--is-ancestor", sha, f"refs/remotes/origin/{base}"],
        capture_output=True,
        check=False,
    )
    return f"PR #{number} merged into {base} as {sha}" if contained.returncode == 0 else None


def repointed(base: str, held: list[str], pr: int) -> None:
    """Before the batch's merges, move the lock to a HELD child of ours naming `pr`, the last PR it merges, leased on
    ours: a lock naming an earlier PR would read as released once that one merged, with the rest still to merge. A
    refused push means another land took the lock meanwhile (it read our PR as merged, merged by hand, or our lock as
    stale), so nothing is merged and theirs is left alone: our commits are dropped from `held`, which nothing then
    releases."""
    with beating:
        if not held:  # the heartbeat found the lock taken
            raise Refused(
                f"another land took the landing lock {lock_name(base)} while this one worked, so nothing was merged"
            )
        ours = held[-1]
        held.append(lock_commit(HELD, ours, pr))
        pushed = leased(base, held[-1], ours)
    if pushed.returncode:
        held.clear()
        raise Refused(
            f"another land took the landing lock {lock_name(base)} before this one's merges, so nothing was merged:"
            f" {' '.join(pushed.stderr.split())}"
        )


def proof_gaps(
    pull: dict[str, Any], tracker: str | None, loop: str | None
) -> tuple[list[str], Any]:
    """What keeps the PR from landing before CI is asked about, as lines to print (none: it holds every proof, and a
    draft may be marked ready, since ready starts CI), with the issues its spec links."""
    where, local = f"PR #{pull['number']}", signed(loop)
    try:
        merge_method(pull, loop)
    except Refused as exc:
        return [str(exc)], None
    if pull["isCrossRepository"]:
        return [
            f"{where} comes from another repository: land merges only branches of this one"
        ], None
    if why := outside(pull, loop):
        # land is the loop's merge: a PR no verifier saw merges its author's way
        return [why], None
    found, issues = gated("merge", pull, tracker, loop)
    if found:
        return holds(pull, issues, loop) + found, issues
    if (proof := head_proof(loop)) and not proved(pull["headRefOid"], proof["status"]):
        names = f", with the tier set in {', '.join(proof['env'])}" if proof["env"] else ""
        gone = (
            f"{where}: its head {pull['headRefOid']} has no green `{proof['status']}` status for its tree (loop.md"
            f" `head_proof`): run `approvals.py local-ci --record --head-proof` on a clean checkout of it{names}"
        )
        return [gone], issues
    if local and (why := unsignable(pull, *local)):
        return [why], issues
    return [], issues


def mark_ready(pull: dict[str, Any], required: set[str]) -> tuple[int, bool] | None:
    """Mark a draft ready (a write: `land`'s own path only, never on a PR `landable` refused, since ready starts CI).
    None when its required checks (if any) need no wait; else the exit its start of CI means."""
    where = f"PR #{pull['number']}"
    # CI skips a draft, and a skipped check counts as green: a draft's checks say nothing about the ready PR
    port("forge").ready(pull["number"])
    print(f"{where} is ready now")
    if required:
        print(f"{where}: CI starts on ready, so its required checks are pending")
        return WAITING, True
    return None


class Verdict(NamedTuple):
    """What `landable` found. `exit`: None when the PR may land, else the exit it means for a named PR and whether a wait
    would clear it. `draft`: a draft that holds every proof, ready to mark (a write for `land`'s own path). `issues`: the
    issues its spec links, None while a proof is missing, so `land` flags a PR's risk only once its proofs hold."""

    exit: tuple[int, bool] | None
    draft: bool = False
    issues: list[dict[str, Any]] | None = None


def readied(
    named: list[dict[str, Any]],
    required: set[str],
    tracker: str | None,
    loop: str | None,
) -> list[dict[str, Any]]:
    """The named PRs, each draft `landable` finds ready to mark flagged and marked ready (and read again) before the
    wait for the lock, so the holder's next batch includes it. A draft it refuses is returned with its verdict in
    `verdict`, which `candidates_of` reuses instead of asking again; a readied one with `readied`, and `waiting` for what its
    start of CI means for this pass."""
    drafts = [pull for pull in named if pull["isDraft"]]
    if not drafts:
        return named
    found: dict[int, dict[str, Any]] = {}
    for pull in drafts:
        verdict = landable(pull, tracker, loop, required, named=True)
        if not verdict.draft:
            found[pull["number"]] = {**pull, "verdict": verdict}
            continue
        for line in flag(pull, verdict.issues or [], loop):
            print(line)
        waiting = mark_ready(pull, required)
        found[pull["number"]] = {
            **pull_request(pull["number"]),
            "readied": True,
            "waiting": waiting,
        }
    return [found.get(pull["number"], pull) for pull in named]


def unreadied(
    named: list[dict[str, Any]], base: str, tracker: str | None, loop: str | None
) -> list[dict[str, Any]]:
    """The named PRs once the policy changed while the land waited for the lock: each draft readied() marked ready that
    `landable` under the new policy would not ready goes back to draft, with a line
    saying why, and is returned as a draft with that verdict, so the batch leaves it out; the rest as they were."""
    required = required_checks(base) - OWN
    found = []
    for pull in named:
        if not pull.get("readied"):
            found.append(pull)
            continue
        draft = {k: v for k, v in pull.items() if k not in ("readied", "waiting")} | {
            "isDraft": True
        }
        verdict = landable(draft, tracker, loop, required, named=True)
        if verdict.draft:
            found.append(pull)
            continue
        port("forge").ready(pull["number"], undo=True)
        print(
            f"PR #{pull['number']} is a draft again: it was readied under the policy the base had before this land's"
            " wait, and the base's policy now would not ready it"
        )
        found.append({**draft, "verdict": verdict})
    return found


def asks_owner(pull: dict[str, Any], lines: list[str], named: bool) -> None:
    """A named PR a land waits on the owner's go for: its `land` event is `needs-owner`, the lines said as the question."""
    if named:
        land_event(pull, "needs-owner", question="\n".join(lines) + "\n")


def landable(
    pull: dict[str, Any],
    tracker: str | None,
    loop: str | None,
    required: set[str],
    named: bool = False,
) -> Verdict:
    """A check, which writes nothing to GitHub: whether the PR is a candidate for the batch (every merge proof, gated,
    no local-ci pass asked, since the verifier's record is the proof and no gate runs at land; no owner wait; the base's required
    checks green on its head; a signed squash possible where loop.md asks for one), or a draft that holds all that and is
    ready to mark, or why not, said once on stdout. The PR is judged as `flag` leaves it (a `risk:high` the gate added
    that `unflag` would take off no longer holds it); `land` writes the label and the ready mark itself. The `named` PR
    with a required check missing, red or pending, or a draft, that GitHub reports conflicting prints its CI lines and
    the fix (merge the base in) and ends the wait: no workflow runs on it, so the check never turns green."""
    where = f"PR #{pull['number']}"
    lines, issues = proof_gaps(pull, tracker, loop)
    if lines:
        for line in lines:
            print(line)
        return Verdict((1, False))
    pull = flagged_as(pull, issues, loop)
    # GitHub runs no workflow on a conflicting PR, so a required check it lacks or has red or pending from before it
    # conflicted never turns green, nor do a draft's once it is ready: before the draft is marked ready, since that
    # would start nothing
    if (
        named
        and required
        and pull.get("mergeable") == "CONFLICTING"
        and (
            pull["isDraft"]
            or required - {name for name, state in head_checks(pull, required)[0] if state in GREEN}
        )
    ):
        for line in ci(where, pull, required):
            print(line)
        base = pull["baseRefName"]
        print(f"{where} conflicts with {base}: merge `origin/{base}` in")
        return Verdict((1, False), issues=issues)
    if pull["isDraft"]:
        if owner := owner_waits(pull, issues, loop):  # ready starts CI, so never before the go
            for line in owner:
                print(line)
            asks_owner(pull, owner, named)
            return Verdict((WAITING, False), issues=issues)
        return Verdict(None, draft=True, issues=issues)
    if pull.get("waiting"):  # readied by this land: its CI has only just started
        return Verdict(pull["waiting"], issues=issues)
    if required and (lines := ci(where, pull, required)):
        covered = breakglass_cover(pull, loop, lines)
        if covered.said:  # the owner's own merge: land verifies it and leaves it
            return breakglass_held(pull, covered, issues, named, loop)
        lines = covered.lines
        for line in lines:
            print(line)
        red = {state for _, state in head_checks(pull, required)[0]} & RED
        wait = (1, False) if red or any(line.endswith(PAGED) for line in lines) else (WAITING, True)
        return Verdict(wait, issues=issues)
    # after CI, so the owner is never asked about a head CI could still reject
    rated = rating(pull, issues, loop)
    print(rated.line(pull), file=sys.stderr)
    if owner := owner_waits(pull, issues, loop):
        for line in owner:
            print(line)
        asks_owner(pull, owner, named)
        return Verdict((WAITING, False), issues=issues)  # the owner is asked, not polled
    if rated.merge != "gates":
        for line in whose(pull, loop):
            print(line)
    return Verdict(None, issues=issues)


# the first line of the comment land leaves on a break-glass PR whose proof it verified, and of the one after the merge
VERIFIED_COMMENT, BREAKGLASS_LAND = "Break-glass verified", "Break-glass land"


def trailer(pull: dict[str, Any], check: str, proof_url: str) -> str:
    """The squash commit's trailer for a break-glass land."""
    return f"Break-glass: owner-approved, {check} red, proof {proof_url}"


def breakglass_held(
    pull: dict[str, Any],
    covered: Covered,
    issues: list[dict[str, Any]],
    named: bool,
    loop: str | None,
) -> Verdict:
    """What land does with a PR whose break-glass proof it verified (breakglass_cover): nothing is merged, since the
    owner merges it in the GitHub UI with the ruleset's bypass. Neither `loop:queued` nor a `land` success stays on it
    (a Mergify queue would merge it on that status: an error replaces it, and the label comes off first); one comment
    carries the verified lines, the proof comment's link and the trailer to paste into the squash message; and the
    owner is asked for the click (the `land` event `needs-owner`)."""
    where, head = f"PR #{pull['number']}", pull["headRefOid"]
    if QUEUED in names(pull):
        label(pull["number"], QUEUED, add=False)
        print(f"{where}: `{QUEUED}` removed: land never queues a break-glass PR")
    try:
        landed = [s for s in statuses(head) if s.get("context") == LAND]
    except (subprocess.CalledProcessError, OSError, ValueError):
        landed = []
    if landed and landed[0].get("state") != "error":
        post_status(
            head, LAND, "Not landed: the owner merges a break-glass PR in the GitHub UI", "error"
        )
    for line in covered.said:
        print(line)
    stand = standing(pull, loop)
    proof_url = (stand.proof or {}).get("url") or pull.get("url", "")
    heading = f"{VERIFIED_COMMENT}"
    if not any(
        machine(note["body"]).strip().splitlines()[:1] == [heading]
        and fields(note["body"]).get("head", "").lower() == head.lower()
        for note in notes(pull)
    ):
        comment(
            pull["number"],
            fold(
                heading,
                f"The proof for {head[:7]} holds, read from the runs. land does not merge it: the owner merges it"
                ' in the GitHub UI with "Merge without waiting for requirements".',
                "\n".join(
                    [
                        f"Head: {head}",
                        f"Proof: {proof_url}",
                        f"Record: {(next((n for n in notes(pull) if parsed(n['body'], BREAKGLASS)), {}) or {}).get('url', '')}",
                        *covered.said[1:],
                        f"Trailer: {trailer(pull, ', '.join(re.findall(r'`([^`]+)`', covered.said[0].split(';')[0])) or 'required', proof_url)}",
                    ]
                ),
            ),
        )
    ask = [
        f"{where}: the break-glass proof holds on {head}: the owner merges it in the GitHub UI (proof {proof_url})"
    ]
    asks_owner(pull, ask, named)
    return Verdict((WAITING, False), issues=issues)


def breakglass_landed(pull: dict[str, Any]) -> None:
    """The `Break-glass land` comment after the owner's merge, from the first land that reads it merged: the head, the
    merge commit, the required checks that were red or missing with their run links, the record and proof links and
    that the owner merged it with the bypass. Nothing for a PR with no break-glass record on its head, or one already
    commented."""
    head = pull["headRefOid"]
    if any(
        machine(note["body"]).strip().splitlines()[:1] == [BREAKGLASS_LAND] for note in notes(pull)
    ):
        return
    records_on = [
        note
        for note in notes(pull)
        if (record := parsed(note["body"], BREAKGLASS))
        and record.get("head", "").lower() == head.lower()
    ]
    if not records_on:
        return
    merged = merged_pull(pull["number"]) or {}
    commit = merged.get("merge_commit_sha") or "unknown"
    red = {name: run for name, run in commit_checks(head).items() if run["conclusion"] in RED}
    required = required_checks(pull["baseRefName"]) - OWN
    lacking = sorted(required - set(commit_checks(head)))
    shown = [f"`{name}` red: {run_link(run['url'])}" for name, run in sorted(red.items())] + [
        f"`{name}` missing" for name in lacking
    ]
    proof = (proofs_on(pull, head) or [{}])[-1].get("url") or pull.get("url", "")
    comment(
        pull["number"],
        fold(
            BREAKGLASS_LAND,
            f"The owner merged {head[:7]} as {commit[:7]} with the ruleset's bypass; land did not merge it.",
            "\n".join(
                [
                    f"Head: {head}",
                    f"Merge: {commit}",
                    f"Checks: {'; '.join(shown) or 'none red or missing now'}",
                    f"Record: {records_on[-1].get('url') or ''}",
                    f"Proof: {proof}",
                    f"Trailer: {trailer(pull, ', '.join(sorted(red) + lacking) or 'required', proof)}",
                ]
            ),
        ),
    )


def candidates_of(
    named: list[dict[str, Any]],
    base: str,
    tracker: str | None,
    loop: str | None,
    required: set[str],
    only: bool,
) -> tuple[list[dict[str, Any]], dict[int, tuple[int, bool]], dict[int, Any]]:
    """Both merge queues' candidates, oldest PR first: every open PR into `base` that is no draft, and the named ones
    (`only`: those alone), each judged by `landable` (its risk flagged once its proofs hold); with the exit each other
    one means for a named PR, and each waiting one's linked issues (batch_wait's owner check)."""
    waits: dict[int, tuple[int, bool]] = {}
    issues: dict[int, Any] = {}  # each waiting PR's linked issues, for batch_wait's owner check
    candidates = []
    listed = {
        other["number"]
        for other in (
            [] if only else open_pulls()
        )  # `only`: no other PR is read, let alone touched
        if other["baseRefName"] == base and not other["isDraft"]
    } | {pull["number"] for pull in named}
    pulls = {pull["number"]: pull for pull in named}
    for number in sorted(listed):
        pull = pulls.get(number) or pull_request(number)
        verdict = pull.get("verdict") or landable(pull, tracker, loop, required, number in pulls)
        # the board shows why a PR waits: flag its risk once its proofs hold, before it is refused for anything else
        if verdict.issues is not None and not pull.get("readied"):
            for line in flag(pull, verdict.issues, loop):
                print(line)
        if verdict.exit is None:
            candidates.append(pull)
        else:
            waits[number] = verdict.exit
            issues[number] = verdict.issues
    return candidates, waits, issues


def outcome(
    named: list[dict[str, Any]],
    exits: dict[int, tuple[int, bool]],
    worst: int,
    failed: set[int],
) -> tuple[int, bool]:
    """A land's exit, and whether a wait would change it: with PRs named the worst of theirs (`exits`, else 1), 1 over 3
    over 0; with none 1 when one `failed`, else 3 while a wait would clear one of `exits`, else 0."""
    if named:
        found = [exits.get(pull["number"], (1, False)) for pull in named]
        code = max((c for c, _ in found), key=lambda c: {1: 2, WAITING: 1}.get(c, 0), default=0)
        code = 1 if worst else code
        return code, code == WAITING and any(again for c, again in found if c == WAITING)
    if failed:
        return 1, False
    return (WAITING, True) if any(again for _, again in exits.values()) else (0, False)


def local_queue(
    named: list[dict[str, Any]],
    base: str,
    tracker: str | None,
    loop: str | None,
    required: set[str],
    only: bool = False,
    held: list[str] | None = None,
    worst: int = 0,
) -> tuple[int, bool]:
    """The local merge queue, under the lock (`held`, its commits): merge every candidate's head onto `base` locally,
    oldest PR first, leaving out a PR that conflicts or that `pre_land` refuses, move the lock to name the last PR to
    land (repointed), and merge each through GitHub in that order. No gate runs: the verifier's record on each head is
    the proof, beside the checks the base requires green on it (landable), so nothing reruns at land (#1872). With no
    queue run to test the merge, a candidate whose head has no pass record (head_recorded) is refused (R6)."""
    candidates, waits, issues = candidates_of(named, base, tracker, loop, required, only)
    candidates = [pull for pull in candidates if head_recorded(pull)]
    waited = ""
    if (
        candidates
        and not only
        and (seconds := settings(loop).get("land", {}).get("batch_wait_seconds", 0))
    ):
        pulls = {pull["number"]: pull for pull in named}
        waited = batch_wait(seconds, pulls, issues, candidates, waits, tracker, loop, required)
    failed: set[int] = set()
    # each PR to land, and the tree it lands
    landed: list[tuple[dict[str, Any], str]] = []
    if candidates and policy_ref(toplevel()) is not None:
        # no working tree here: the batch is computed in objects
        if settings(loop).get("pre_land"):
            raise Refused(
                f"loop.md's `pre_land` runs in a checkout of {base}'s tip, and this clone has no working tree:"
                " land where the files are checked out"
            )
        tip = git("rev-parse", f"refs/remotes/origin/{base}").strip()
        landed = merged_trees(candidates, base, tip, failed)
        # []: a lock the heartbeat lost, which repointed() refuses; None: no lock
        if landed and held is not None:
            repointed(base, held, landed[-1][0]["number"])
        if landed:
            merges(landed, base, tip, loop, failed, waited, held)
    elif candidates:
        with batch_tree(base) as work:
            tip = run_in(work, "rev-parse", "HEAD")
            candidates = [pull for pull in candidates if pre_landed(pull, base, work, loop, failed)]
            landed = merge_batch(candidates, base, work, failed)
            # []: a lock the heartbeat lost, which repointed() refuses; None: no lock
            if landed and held is not None:
                repointed(base, held, landed[-1][0]["number"])
            if landed:
                merges(landed, base, tip, loop, failed, waited, held)
    # each named PR's exit: its wait's, else 0 when it merged, else 1
    done = {pull["number"] for pull, *_ in landed} - failed
    return outcome(named, {number: (0, False) for number in done} | waits, worst, failed)


def tests_record_words(point: str, head: str | None = None) -> list[str] | None:
    """The words `gate-record <head> -- <words>` takes for the record of a run of a PR's tests: loop.md's `Affected
    tests:` command narrowed against `point` (narrowed_gate under narrowed_to: the PR's merge base with its base
    branch, the base `affected-tests --record` selects against and keys its record by), its last word `(narrowed)`;
    None where no narrowed run exists (no `Affected tests:` line, `Gate: full`, `gate_steps`). The one builder of that
    key for `land` (head_recorded) and the merge queue's floor (the project's batch selector), so neither drifts from
    the writer. `head` is the PR's head, so the cut (selected) classifies the PR's change, not the checkout's."""
    with narrowed_to(point, head):
        narrowed = narrowed_gate()
    return None if narrowed is None else shlex.split(narrowed[0])


def head_recorded(pull: dict[str, Any]) -> bool:
    """Whether origin holds the record of a run of the PR's tests on its head (gate_record): the local queue runs no
    test, so a merge with neither a queue run nor a record of each PR is refused (engineering-loop SKILL.md, R6),
    naming the missing record and the command that makes it; a named PR left out exits 1 (outcome). The record is the
    one `affected-tests --record` writes (tests_record_words), so a record of the gate alone, which may run no test,
    lands nothing. A project with no narrowed run (no `Affected tests:` line, `Gate: full`, `gate_steps`) has no such
    record, and its gate is the run that tests the PR, so there the gate's record (`local-ci --record`) is read, as
    before. A break-glass PR never gets here: landable holds it for the owner's own merge."""
    head = pull["headRefOid"]
    where = f"PR #{pull['number']}: not landed: no pass record on its head {head[:12]}"
    words, writer = [], "local-ci --record"
    if narrowing() is not None:
        if (point := merge_base_of(pull["baseRefName"], head)) is None:
            print(
                f"{where}: no merge base with origin/{pull['baseRefName']}, so no `Affected tests:` record is found"
            )
            return False
        if (narrowed := tests_record_words(point, head)) is not None:
            words, writer = narrowed, "affected-tests --record"
    said = io.StringIO()
    with contextlib.redirect_stdout(said):
        code = gate_record(head, words)
    if code == 0:
        return True
    print(
        f"{where} ({' '.join(said.getvalue().split())}); run `approvals.py {writer}` on that head, which records one,"
        " then land again"
    )
    return False


def check_never_comes(pull: dict[str, Any], required: set[str]) -> bool:
    """Whether a required check of the PR's head will not come on its own, so waiting for it is pointless: GitHub runs no
    workflow on a conflicting PR missing one (landable ends a named PR's wait the same way), and a required check whose
    only run is the draft's skip waits for good where the workflow lacks the `ready_for_review` type (ci()'s line)."""
    checks, _, drafts = head_checks(pull, required)
    conflicting = pull.get("mergeable") == "CONFLICTING"
    return bool(required & drafts) or (
        conflicting and bool(required - {name for name, _ in checks})
    )


def batch_wait(
    seconds: int,
    pulls: dict[int, dict[str, Any]],
    issues: dict[int, Any],
    candidates: list[dict[str, Any]],
    waits: dict[int, tuple[int, bool]],
    tracker: str | None,
    loop: str | None,
    required: set[str],
) -> str:
    """Under the lock, before the batch tree: wait up to `seconds` (loop.md's `batch_wait_seconds`) while another PR is
    held by nothing but its required checks still running (landable's (WAITING, True), no owner wait behind it, and no
    check that never comes: check_never_comes), so
    it lands in this batch instead of waiting for the next land. Each one that turns landable joins `candidates`
    (oldest first) and leaves `waits`; one that turns red, or asks the owner, is dropped from the wait. Returns the
    text for the Landed comment's `Batch:` line, empty when nothing was waited for."""
    near = {
        number
        for number, exit in waits.items()
        if exit == (WAITING, True)
        and not check_never_comes(pulls.get(number) or pull_request(number), required)
        and not owner_waits(pulls.get(number) or pull_request(number), issues[number] or [], loop)
    }
    if not near:
        return ""
    start = time.monotonic()
    deadline = start + seconds
    print(
        f"land waits up to {seconds} s for the required checks of "
        + ", ".join(f"#{n}" for n in sorted(near))
        + " before it lands the batch"
    )
    joined: list[int] = []
    while near and time.monotonic() < deadline:
        time.sleep(min(QUEUE_POLL, max(0.0, deadline - time.monotonic())))
        github.read_afresh()  # the next read is GitHub's, not this run's memory (#2016)
        for number in sorted(near):
            pull = pull_request(number)
            with contextlib.redirect_stdout(
                io.StringIO()
            ):  # why a PR still waits is no news every poll
                verdict = landable(pull, tracker, loop, required, number in pulls)
            if verdict.exit is None:
                candidates.append(pull)
                waits.pop(number)
                joined.append(number)
                near.remove(number)
                print(f"PR #{number} turned landable and joins the batch")
            elif not (
                verdict.exit == (WAITING, True)
                and not check_never_comes(pull, required)
                and not owner_waits(pull, verdict.issues or [], loop)
            ):
                waits[number] = verdict.exit
                near.remove(number)
                print(f"PR #{number} no longer waits only on its checks: left out of the wait")
    candidates.sort(key=lambda pull: pull["number"])
    spent = round(time.monotonic() - start)
    who = ", ".join(f"#{n}" for n in joined) or "none"
    print(f"land waited {spent} s for the batch; joined: {who}")
    return f" (waited {spent} s, joined: {who})"


@contextlib.contextmanager
def batch_tree(base: str) -> Any:
    """A detached worktree of origin's `base` under the git common dir, removed after; its gate logs stay beside it.
    Under the common dir rather than the system's temp dir, so a tool that trusts the clone's path trusts it too."""
    root = (
        Path(git("rev-parse", "--path-format=absolute", "--git-common-dir").strip())
        / "approvals"
        / "landing"
    )
    root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(dir=root))
    git("worktree", "add", "-q", "--detach", str(work), f"refs/remotes/origin/{base}")
    try:
        yield work
    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(work)], capture_output=True, check=False
        )
        shutil.rmtree(work, ignore_errors=True)
        subprocess.run(["git", "worktree", "prune"], capture_output=True, check=False)


def merge_batch(
    candidates: list[dict[str, Any]], base: str, work: Path, failed: set[int]
) -> list[tuple[dict[str, Any], str]]:
    """Merge the candidates onto the base in `work`, a PR that conflicts left out (merged_onto); no gate runs on the
    result. Each PR to land, with the tree it lands: the base with it and the PRs before it merged."""
    landed: list[tuple[dict[str, Any], str]] = []
    for pull in candidates:
        if merged_onto(pull, work, base, [other["number"] for other, _ in landed], failed):
            landed.append((pull, run_in(work, "rev-parse", "HEAD^{tree}")))
    return landed


def run_in(work: Path, *args: str) -> str:
    return git("-C", str(work), "-c", "commit.gpgsign=false", *args).strip()


def reset(work: Path, commit: str) -> None:
    """`work` at `commit` with nothing a check (`pre_land`) left behind but ignored files, such as caches, so the batch
    merges onto the commit's tree."""
    run_in(work, "reset", "-q", "--hard", commit)
    run_in(work, "clean", "-fdq")


CONFLICTS = "Conflicts in the landing batch"
PRE_LAND = "Refused by the pre-land check"


def merged_onto(
    pull: dict[str, Any], work: Path, base: str, before: list[int], failed: set[int]
) -> bool:
    """Merge the PR's head into `work` (`git merge --no-ff`); a conflict is aborted and said once per head on the PR,
    since it needs the base merged in. `before` names the PRs merged ahead of it."""
    where, head = f"PR #{pull['number']}", pull["headRefOid"]
    if not fetched(head):
        print(f"{where}: its head {head} is not on origin: push it, then land again")
        failed.add(pull["number"])
        return False
    try:
        run_in(work, "merge", "-q", "--no-ff", "--no-edit", head)
        return True
    except subprocess.CalledProcessError:
        subprocess.run(
            ["git", "-C", str(work), "merge", "--abort"], capture_output=True, check=False
        )
    skip_conflict(pull, base, before, failed)
    return False


def skip_conflict(pull: dict[str, Any], base: str, before: list[int], failed: set[int]) -> None:
    """Leave out a PR whose head conflicts with the batch, saying so once per head on the PR."""
    where = f"PR #{pull['number']}"
    failed.add(pull["number"])
    ahead = f" plus {', '.join(f'#{n}' for n in before)} merged before it" if before else ""
    print(f"{where} conflicts with {base}{ahead}: skipped")
    conflict_note(pull, base, ahead)


def conflict_note(pull: dict[str, Any], base: str, ahead: str = "") -> None:
    """Say on the PR, once per head, that it conflicts with `base` (`ahead`: and the PRs merged before it), so it needs
    the base merged in: both merge queues' comment."""
    head = pull["headRefOid"]
    if not any(
        note["body"].startswith(CONFLICTS) and f"Head: {head}" in note["body"]
        for note in notes(pull)
    ):
        comment(
            pull["number"],
            fold(
                CONFLICTS,
                f"It conflicts with {base}{ahead}: merge origin/{base} in, rerun the gates, get a verdict on the"
                " merge (the lead role, step 8), then land again.",
                f"Head: {head}",
            ),
        )


def merged_trees(
    candidates: list[dict[str, Any]], base: str, tip: str, failed: set[int]
) -> list[tuple[dict[str, Any], str]]:
    """merge_batch for a clone with no working tree: each candidate's head merged onto `tip` (origin's `base`) in
    objects alone, `git merge-tree --write-tree` and `git commit-tree`, so no file is checked out."""
    commit, landed = tip, []
    for pull in candidates:
        head = pull["headRefOid"]
        if not fetched(head):
            print(
                f"PR #{pull['number']}: its head {head} is not on origin: push it, then land again"
            )
            failed.add(pull["number"])
            continue
        made = subprocess.run(
            ["git", "merge-tree", "--write-tree", "--no-messages", commit, head],
            capture_output=True,
            text=True,
            check=False,
        )
        if made.returncode == 1:
            skip_conflict(pull, base, [p["number"] for p, _ in landed], failed)
            continue
        if made.returncode:
            raise Refused(
                f"git merge-tree {commit} {head}: {made.stderr.strip() or made.returncode}"
            )
        tree = made.stdout.split()[0]
        commit = git(
            "-c",
            "commit.gpgsign=false",
            "commit-tree",
            tree,
            "-p",
            commit,
            "-p",
            head,
            "-m",
            f"Merge {head}",
        ).strip()
        landed.append((pull, tree))
    return landed


class PreLand(NamedTuple):
    """One run of loop.md's `pre_land` on a PR: the command as written, the base's tip it ran at, its exit and its
    output (stdout and stderr together)."""

    command: str
    tip: str
    exit: int
    output: str

    def tail(self) -> str:
        """Its output's last LOG_TAIL lines, each indented on a line of its own, where a check says why."""
        return "".join(f"\n  {line}" for line in self.output.rstrip("\n").splitlines()[-LOG_TAIL:])


def pre_land_run(pull: dict[str, Any], work: Path, loop: str | None) -> PreLand | None:
    """Run loop.md's `pre_land` on the PR in `work`, a checkout of the base's tip, with LAND_HEAD (the PR's head) and
    LAND_BASE (that tip) set, so the base's tree holds the check that judges a PR, never the PR's own. None when loop.md
    sets none, or the head isn't on origin (the check couldn't read it: the caller says so). The one run `land`
    (pre_landed) and `check merge` (pre_land_holds) share, so they can't disagree on what it passes."""
    if not (written := settings(loop).get("pre_land")):
        return None
    command, words, sets = command_line(written, "pre_land")
    head, tip = pull["headRefOid"], run_in(work, "rev-parse", "HEAD")
    # so the check can read the head
    if not fetched(head):
        return None
    try:
        code, output = run_heavy(
            words,
            cwd=work,
            env=os.environ | sets | {"LAND_HEAD": head, "LAND_BASE": tip},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
        )
    except OSError as exc:
        raise Refused(f"loop.md's `pre_land` {command!r} can't run: {exc}") from None
    return PreLand(command, tip, code, output or "")


def pre_landed(
    pull: dict[str, Any], base: str, work: Path, loop: str | None, failed: set[int]
) -> bool:
    """Whether loop.md's `pre_land` passes the PR (pre_land_run), True without one. Non-zero leaves the PR out of this
    batch with its output's end printed on every land and said once per head on the PR; it isn't red, so it stays
    ready."""
    ran = pre_land_run(pull, work, loop)
    if ran is None:
        return True  # a head not on origin is merged_onto's to report
    reset(work, ran.tip)  # the batch merges onto the base as it is, whatever the check left
    if not ran.exit:
        return True
    where, head = f"PR #{pull['number']}", pull["headRefOid"]
    failed.add(pull["number"])
    # its output's end on every land, not only in the comment, which is once per head: a land again on the same head
    # would otherwise say only the exit code
    print(
        f"{where}: `{ran.command}` refused it against {base} at {ran.tip}, exiting {ran.exit}: left out of this batch"
        + ran.tail()
    )
    if not any(
        note["body"].startswith(PRE_LAND) and f"Head: {head}" in note["body"]
        for note in notes(pull)
    ):
        comment(
            pull["number"],
            fold(
                PRE_LAND,
                f"`{ran.command}` refused it against {base} at {ran.tip}, exiting {ran.exit}: fix what it says, then"
                " land again.",
                f"Head: {head}\n\n````\n"
                # its end, where a check says why, within GitHub's 65536 characters a comment holds
                + ran.output.rstrip("\n")[-60000:]
                + "\n````",
            ),
        )
    return False


def pre_land_holds(pull: dict[str, Any], loop: str | None, rest_holds: bool) -> list[str]:
    """`check merge`'s lines for loop.md's `pre_land`, so it never calls clear a PR `land` would leave out: none
    without one. While something else holds the merge (`rest_holds`), one line that it has not run yet, since land runs
    it only on a candidate; else it runs as land runs it (pre_land_run), in a temporary worktree of the base's tip, and
    a refusal is one line with its output's end. From a clone with no working tree, where land refuses a `pre_land`,
    one line saying so. Each line is a wait, not a failure: land leaves the PR out but keeps it ready (#2063)."""
    if not (written := settings(loop).get("pre_land")):
        return []
    where, base = f"PR #{pull['number']}", pull["baseRefName"]
    command = command_line(written, "pre_land")[0]
    if rest_holds:
        return [
            f"{where}: pre_land not run: `{command}` (land runs it once nothing else holds the merge)"
        ]
    if policy_ref(toplevel()) is not None:
        return [
            (
                f"{where}: pre_land not run: `{command}` runs in a checkout of {base}'s tip, and this clone has no"
                " working tree, so land refuses it here too: check merge where the files are checked out"
            )
        ]
    if not fetched(pull["headRefOid"]):
        return [
            f"{where}: pre_land not run: its head {pull['headRefOid']} is not on origin: push it"
        ]
    with batch_tree(base) as work:
        ran = pre_land_run(pull, work, loop)
    if ran is None or not ran.exit:
        return []
    return [
        f"{where}: `{ran.command}` refuses it against {base} at {ran.tip}, exiting {ran.exit}: land leaves it out"
        + ran.tail()
    ]


def evicted(
    pull: dict[str, Any], title: str, said: str, why: str, tail: list[str], what: str
) -> None:
    """Take a PR red twice out of either merge queue: back to draft, so the next land leaves it out until `land <pr>`
    marks it ready again, and a comment headed `title` saying `why` it failed, holding `tail`, the last LOG_TAIL lines
    of the log `what` names."""
    where, head, number = f"PR #{pull['number']}", pull["headRefOid"], pull["number"]
    print(f"{where} {said}: evicted")
    port("forge").ready(number, undo=True)
    posted = comment(
        number,
        fold(title, f"{why}: fix it, then `land {number}` marks it ready again.", f"Head: {head}")
        + "\n"
        + folded(what, tail),
    )
    land_event(pull, "failed", url=posted if isinstance(posted, str) else "")


def folded(what: str, lines: list[str]) -> str:
    """A comment's closing block of log `lines`, folded under `what`."""
    return (
        f"<details><summary>{what}</summary>\n\n````\n" + "\n".join(lines) + "\n````\n</details>\n"
    )


# how long an entry may wait for checks with no check suite on its commit: GitHub lost the merge_group event (#404)
STUCK = 10 * 60
# RemovedFromMergeQueueEvent.reason, a String in GitHub's schema: these values are read off real removals, beside
# `checks_timed_out` and `manual`, which count for nothing
FAILED_CHECKS, MERGE_CONFLICT, QUEUE_MERGED = "failed_checks", "merge_conflict", "merged"
QUEUE_RED = "Red in the merge queue"
QUEUE_EVICTED = "Evicted from the merge queue"
LANDED = "Landed"
# the local merge queue's `Landed` comment's `Gate:` line: no gate runs at land, the verifier's record is the proof
NO_GATE = "none at land, on the verifier's record of the head"
# the label land puts on each PR it enqueues, and takes off once the PR's `Landed` comment is posted (queue_landed)
QUEUED = "loop:queued"


def github_queue(
    named: list[dict[str, Any]],
    base: str,
    tracker: str | None,
    loop: str | None,
    required: set[str],
    method: str,
    only: bool = False,
    worst: int = 0,
) -> tuple[int, bool]:
    """The GitHub merge queue, which `base`'s rules require (merging with `method`): no landing lock, local batch or
    local gate, since GitHub's queue orders the entries, gates each group with its `merge_group` CI run and isolates a
    red one. Every candidate (candidates_of), oldest PR first, goes in pinned to its head (queue_step). A PR in
    the queue is done (exit 0), and land exits; `--wait` waits on a named one until the queue merges it (exit 0) or
    takes it out (the round then reads the removal), reading only its queue state each QUEUE_WATCH (#2101). Exit 3
    stays for a PR GitHub is still working out a conflict of, or a base still running its checks, which a later `land`
    reads again."""
    if signed(loop):
        raise Refused(
            f"{base}'s rules require GitHub's merge queue, which merges with GitHub's signature, but loop.md's"
            " `land.signed_squash_to` asks for this checkout's: unset it, or drop the queue from the ruleset"
        )
    git("fetch", "-q", "origin", f"+refs/heads/{base}:refs/remotes/origin/{base}")
    judge_with_base(base)
    # GitHub's queue batches the PRs itself, so no batch_wait
    candidates, exits, _ = candidates_of(named, base, tracker, loop, required, only)
    numbers, failed = {pull["number"] for pull in named}, set()
    for pull in candidates:
        code, queued = queue_step(pull, base, method, loop, required)
        if code == 1:
            failed.add(pull["number"])
        if waiting and code == 0 and queued and pull["number"] in numbers:
            # land --wait waits on until the queue merges it or takes it out (#2101), reading only its queue state
            watched["queued"][pull["number"]] = GITHUB
            exits[pull["number"]] = (WAITING, True)
        elif pull["number"] in numbers or code != WAITING:
            # what candidates_of waits on (a check, an owner's go) keeps its own wait
            exits[pull["number"]] = (code, False)
    if not named:
        queue_trail(base)
    return outcome(named, exits, worst, failed)


def queue_trail(base: str) -> None:
    """A bare land's `Landed` comments (queue_landed) on the merged PRs into `base` that still carry QUEUED, since no
    `land <pr>` may ever read them merged. A PR whose trail fails keeps the label, says why and is tried again on the
    next land; it never fails this one, whose candidates are already in."""
    try:
        listed: list[int] = port("forge").merged_labelled(base, QUEUED)
    except subprocess.CalledProcessError as exc:
        print(f"land could not list the PRs merged into {base}: {stderr_of(exc)}")
        return
    for number in listed:
        try:
            queue_landed(pull_request(number))
        except (Refused, subprocess.CalledProcessError) as exc:
            print(f"PR #{number}: land could not write its `{LANDED}` comment: {stderr_of(exc)}")


# `land.queue: mergify`: the label Mergify's `dequeued_label` puts on a PR it took out of its queue, and what the
# `Landed` comment's `Gate:` line names for a PR it merged
MERGIFY, DEQUEUED, MERGIFY_GATE = "mergify", "loop:dequeued", "Mergify merge queue"
MERGIFY_QUEUED = "Queued for Mergify at {}"


def mergify(loop: str | None) -> bool:
    """Whether the policy names Mergify as the base's merge queue (`land.queue: mergify`); refused with
    `land.signed_squash_to`, since Mergify merges through GitHub, which signs with its own key."""
    land = settings(loop).get("land") or {}
    if land.get("queue") != MERGIFY:
        return False
    if "signed_squash_to" in land:
        raise Refused(
            f"{source(loop)}'s `land.queue: mergify` has Mergify merge each PR through GitHub, which signs with its"
            " own key, but `land.signed_squash_to` asks for this checkout's: set one of them"
        )
    return True


GITHUB = "github"


def held_by(loop: str | None, rules: set[str]) -> str:
    """Which queue merges the base's PRs: GITHUB where its rules carry a `merge_queue` rule (they win over the policy),
    else MERGIFY where the policy names it (mergify), else `land`, which merges each PR itself. A policy naming
    GitHub's queue (`land.queue: github`) on a base whose rules carry none is refused, since `land` would then merge
    directly while the policy says a queue does."""
    if getattr(rules, "queue", None):
        return GITHUB
    if mergify(loop):
        return MERGIFY
    if (settings(loop).get("land") or {}).get("queue") == GITHUB:
        raise Refused(
            f"{source(loop)}'s `land.queue: github` names GitHub's merge queue, but the base's rules require none, so"
            " land would merge each PR itself: add a `merge_queue` rule to the base's ruleset, or drop the setting"
        )
    return "land"


def mergify_queue(
    named: list[dict[str, Any]],
    base: str,
    tracker: str | None,
    loop: str | None,
    required: set[str],
    only: bool = False,
    worst: int = 0,
) -> tuple[int, bool]:
    """The Mergify merge queue the policy names (`land.queue: mergify`), on a base whose rules require no GitHub merge
    queue: as github_queue, no landing lock, local batch or local gate, since Mergify batches the queued PRs, tests
    each batch as a draft PR on top of `base` in CI and squash-merges on green. Every candidate (candidates_of), oldest
    PR first, is handed over with the `land` status on its head and QUEUED (mergify_step), which `.mergify.yml`'s
    queue conditions read, and land exits; `--wait` waits on a named one until Mergify merges it (exit 0) or dequeues it
    (DEQUEUED: the round then evicts it), reading only its state and labels each QUEUE_WATCH (#2101). A bare land
    writes the `Landed` trail of the PRs Mergify merged (queue_trail). Every call of a round is REST."""
    git("fetch", "-q", "origin", f"+refs/heads/{base}:refs/remotes/origin/{base}")
    judge_with_base(base)
    candidates, exits, _ = candidates_of(named, base, tracker, loop, required, only)
    failed = set()
    numbers = {pull["number"] for pull in named}
    for pull in candidates:
        code = mergify_step(pull, loop)
        if code == 1:
            failed.add(pull["number"])
        if waiting and not code and pull["number"] in numbers:
            # land --wait waits on until Mergify merges it or dequeues it (#2101), reading only its state and labels
            watched["queued"][pull["number"]] = MERGIFY
            exits[pull["number"]] = (WAITING, True)
        else:
            exits[pull["number"]] = (code, False)
    if not named:
        queue_trail(base)
    return outcome(named, exits, worst, failed)


def queued_here(pull: dict[str, Any]) -> bool:
    """Whether land's current hand-over to Mergify is at the PR's head (mergify_step): the newest `land` status there is
    a success, written after land's newest eviction at that head (evicted_at). An eviction whose `error` status was
    refused (a cloud session, unwritten) leaves the older success newest, and Mergify labels DEQUEUED after every
    draft of a PR it held, land's own eviction too: read as a hand-over, that success would evict the re-land again."""
    head = pull["headRefOid"]
    found = next((each for each in statuses(head) if each.get("context") == LAND), None)
    if not found or found.get("state") != "success":
        return False
    evicted = evicted_at(pull, head)
    # ISO 8601 in UTC, so it orders as text; a status with no time is read as the hand-over, as before
    return evicted is None or not found.get("created_at") or found["created_at"] > evicted


def evicted_at(pull: dict[str, Any], head: str) -> str | None:
    """When land last evicted the PR from Mergify's queue at `head`: its newest QUEUE_EVICTED comment naming that head,
    which is written even where the `land` `error` status is refused. A `Timed out twice` stop (#2308) leaves the head
    out of the queue as an eviction does, so a hand-over after it asks Mergify to queue the head again."""
    times = [
        note.get("createdAt") or ""
        for note in notes(pull)
        if machine(note["body"]).strip().splitlines()[:1] in ([QUEUE_EVICTED], [TIMED_OUT_TWICE])
        and fields(note["body"]).get("head", "").lower() == head.lower()
    ]
    return max(times) if times else None


def hand_over(number: int) -> None:
    """The end of `approve merge` and `record verdict` under `land.queue: mergify`: when `check merge` passes on the PR's
    head, land it (`land --only`, so the hand-over is mergify_step's, and a PR already queued at its head is left
    alone); when something is still missing, print what it is (checked) and queue nothing. A draft, a closed PR and a
    base with another queue are left to `land`, as before. The record is already written, so a refusal here changes
    no exit code."""
    pull = pull_request(number)
    if pull["state"] != "OPEN" or pull["isDraft"]:
        return
    with policy_from(pull["baseRefName"]):
        if not mergify(project()[1]):
            return
        if checked("merge", pull):
            print(f"PR #{number} is not queued: what is missing is above")
            return
        if landing([number], only=True)[0]:
            print(f"PR #{number} is not queued: what holds it is above")


def stalled() -> int:
    """`check stalled`: each open PR, into any base, that `landable` finds a candidate (its go on its head, every proof,
    a satisfied verdict) and that its base's queue does not hold: under `land.queue: mergify` not queued at its head
    (QUEUED and a `land` success there newer than land's last eviction at it, queued_here); where the base's rules
    require GitHub's merge queue, not in it; on a base `land` merges itself, still open, so not landed. A `land` that was stopped, died or never ran left it.
    Also each one `land` refuses whose newest pass on its head is satisfied (or at the cap) but that lacks the go or
    the gate record it needs and has waited past `loop.stalled_after_hours` with no go, record or new head
    (idle_stall), its line naming what is missing: a hand-back that lived only in a session's prose left it.
    One line each, and exit 1 when any is listed, so a sweep can land it (`land <n> --only`) or flag it; judged as
    `land` judges (by the base's policy, read from `origin/<base>`, and only where the gatekeeper code running here is
    the base's: judge_with_base without its policy-file check, since a checkout's own edit of those files is never
    read), and written nowhere. A PR that can't be judged from this checkout (its base's code differs from what runs
    here, as a PR stacked on another's branch has, or GitHub refused a read) gets one `not judged` line and is skipped; the rest are still judged, a last line counts
    the skipped (`<n> of <m> open PRs not judged here`), and a skip alone changes no exit."""
    bases: dict[str, tuple[str | None, str | None, set[str], str] | str] = {}
    lines, found, skipped, seen = [], False, 0, 0
    for listed in sorted(open_pulls(), key=lambda each: each["number"]):
        if listed["isDraft"]:
            continue
        number, base = listed["number"], listed["baseRefName"]
        try:
            if base not in bases:
                bases[base] = stalled_base(base)
            if isinstance(known := bases[base], str):
                raise Refused(known)
            tracker, loop, required, queue = known
            with policy_from(base):
                # its policy is origin/<base>'s (project()) and it writes nothing: a lead's worktree that edits or lags
                # the policy files still judges, and only the code running here must be the base's
                judge_with_base(base, rules=False)
                pull = pull_request(number)
                with contextlib.redirect_stdout(io.StringIO()):
                    judged = landable(pull, tracker, loop, required)
        except (Refused, subprocess.CalledProcessError) as exc:
            if base in bases and not isinstance(bases[base], str) and isinstance(exc, Drifted):
                bases[base] = str(
                    exc
                )  # the base's code differs for every PR into it: said once per PR, not fetched again
            why = str(exc) if isinstance(exc, Refused) else stderr_of(exc)
            lines.append(f"PR #{number} into {base} is not judged here: {why}")
            skipped += 1
            continue
        seen += 1
        head = pull["headRefOid"]
        if pull["state"] != "OPEN":
            continue
        if judged.exit is not None:
            with policy_from(base), contextlib.redirect_stdout(io.StringIO()):
                idle = idle_stall(pull, loop)
            if idle:
                found = True
                lines.append(idle)
            continue
        if queue == MERGIFY:
            if QUEUED in names(pull) and queued_here(pull):
                continue
            left = "is not queued"
        elif queue == GITHUB:
            if pull.get("mergeQueueEntry") or pull.get("isInMergeQueue"):
                continue
            left = "is not in the merge queue"
        else:
            left = "is not landed"
        found = True
        lines.append(f"PR #{number} has its go and a satisfied verdict at {head} and {left}")
    if skipped:  # so a reader tells a listing that judged nothing from one that found nothing
        lines.append(f"{skipped} of {skipped + seen} open PRs not judged here")
    for line in lines:
        print(line)
    return 1 if found else 0


# `check stalled`'s default for `loop.stalled_after_hours`: how long a PR may hold a satisfied verdict on its head with
# no go, no gate record and no new head before it is listed as waiting on what it lacks
STALLED_HOURS = 4


def idle_stall(pull: dict[str, Any], loop: str | None) -> str | None:
    """`check stalled`'s line for a PR `landable` refuses that waits only on a person or a record: its newest verifier
    pass on the head (passes) is satisfied with no blocker or major (held), or is at the cap or past it (cap), and it
    still lacks the go its rating asks (owner_waits) or the gate record `loop.head_proof` asks on the head (proved), while its
    head was pushed, and its newest verdict there posted, more than `loop.stalled_after_hours` (default STALLED_HOURS)
    ago (head_pushed, the review's `submittedAt`), so no go, record, push or verdict came in that time. None for any other PR: one refused for something else is not stalled on a hand-back."""
    head = pull["headRefOid"]
    found = passes(pull)
    if isinstance(found, str):
        return None
    on_head = [(number, lines) for commit, number, lines in found if commit == head.lower()]
    if not on_head:
        return None
    last = max(number for number, _ in on_head)
    satisfied = all(held(lines) for number, lines in on_head if number == last)
    if not satisfied and last < cap(loop):
        return None
    missing = ["go"] if owner_waits(pull, rated_issues(pull), loop) else []
    if (proof := head_proof(loop)) and not proved(head, proof["status"]):
        missing.append("record")  # the head's gate record `land` reads (proof_gaps), not a batch's
    if not missing:
        return None
    # the later of the head's push and the newest verdict on it: a pass just posted on an old head has had no time yet
    # for its hand-back (a submitted review always has its time; one without, still pending, is no verdict)
    moves = [head_pushed(pull) or ""] + [
        review["submittedAt"]
        for review in pull["reviews"]["nodes"]
        if review.get("submittedAt")
        and PASS.match(review.get("body") or "")
        and ((review.get("commit") or {}).get("oid") or "").lower() == head.lower()
    ]
    if not moves[0]:
        return None
    since = max(datetime.fromisoformat(at.replace("Z", "+00:00")) for at in moves)
    waited = datetime.now(timezone.utc) - since
    hours = waited.total_seconds() / 3600
    if hours <= settings(loop).get("stalled_after_hours", STALLED_HOURS):
        return None
    verdict = "a satisfied verdict" if satisfied else f"its pass {last} verdict, at the cap,"
    return (
        f"PR #{pull['number']} has {verdict} at {head} and no go, gate record or new head for {hours:.0f}h:"
        f" missing: {', '.join(missing)}"
    )


def stalled_base(base: str) -> tuple[str | None, str | None, set[str], str] | str:
    """What `check stalled` judges every PR into `base` by, read once: its tracker and policy, its required checks
    (OWN aside) and which queue holds a candidate (MERGIFY, `github` where its rules require GitHub's merge queue, else
    `land`, which merges it itself); or, when the base can't be fetched or its policy is refused, why, which each PR
    into it is skipped with."""
    try:
        git("fetch", "-q", "origin", f"+refs/heads/{base}:refs/remotes/origin/{base}")
        with policy_from(base):
            tracker, loop = project()
            rules = required_checks(base)
            queue = held_by(loop, rules)
    except subprocess.CalledProcessError as exc:
        return f"could not fetch or read {base}: {stderr_of(exc)}"
    except Refused as exc:
        return str(exc)
    return tracker, loop, rules - OWN, queue


def mergify_step(pull: dict[str, Any], loop: str | None) -> int:
    """One candidate for Mergify, and its exit: dequeued by Mergify at the head land queued (DEQUEUED, Mergify's
    `dequeued_label`, after its own retry of a red batch, or on a conflict), evicted (exit 1), unless its queue check
    run says the dequeue was a timeout (mergify_timeout): that one is no test's failure, so the label comes off and
    REQUEUE takes it back, evicting no one (exit 0), and a `Timed out in the merge queue` record names the head and
    `timeouts.run`; at the head's second timeout under that `timeouts.run` land instead stops (exit 3, a wait on a
    person): `land` `error` on the head, both labels off, a `Timed out twice in the merge queue` record (`Head:`,
    `Run:`) and one line naming `timeouts.run`, held out by the next lands until a new head, a ready-again (which
    starts the count anew) or a changed `timeouts.run` (#2308); already queued at its head
    (QUEUED and a `land` success there since land's last eviction at it, queued_here), left alone (exit 0); else
    queued: the `land` success on its head, which binds the hand-over to that one head, so a push leaves
    `.mergify.yml`'s conditions unmet, and QUEUED (exit 0). Either way, at a head land evicted before, REQUEUE once
    per hand-over (requeue), so a command GitHub refused is posted by the next land. A DEQUEUED label on a head land
    does not hold queued (a push since, or land's own eviction, which Mergify labels too) is stale, and comes off."""
    where, head, number = f"PR #{pull['number']}", pull["headRefOid"], pull["number"]
    labels = {each["name"] for each in pull["labels"]["nodes"]}
    try:
        budget = timeouts(loop)["run"]
        if timed_out_twice(pull, budget):
            # two timeouts at this head under this budget: held out of the queue until the budget moves (#2308)
            for name in (QUEUED, DEQUEUED):
                if name in labels:
                    label(number, name, add=False)
            print(timeout_stop_line(where, head, budget))
            return WAITING
        ours = queued_here(pull)
        if DEQUEUED in labels:
            found = mergify_timeout(head) if ours else None
            first = timeout_notes(pull, head, TIMED_OUT_ONCE, budget)
            if found and found[1] and found[1] < timeout_recorded_at(pull, head):
                # the check run's timeout text is older than the head's newest timeout record, under whatever budget
                # and before whatever ready: this dequeue is another's
                found = None
            if found:
                timeout = found[0]
                if first:
                    # the second timeout at this head: stop requeueing, one line, a wait on a person (#2308)
                    unwritten(head, LAND, "Timed out twice in the Mergify merge queue", "error")
                    comment(
                        number,
                        fold(
                            TIMED_OUT_TWICE,
                            timeout_stop_line(where, head, budget),
                            f"Head: {head}\nRun: {said_span(budget)}",
                        ),
                    )
                    for name in (QUEUED, DEQUEUED):
                        if name in labels:
                            label(number, name, add=False)
                    print(timeout_stop_line(where, head, budget))
                    return WAITING
                # a timeout is no test's failure (#2228): queue it again later, evict no one
                # the command first: refused, the label stays for the next land to try again
                comment(number, REQUEUE + "\n")
                # the label off, then the record: a refused step leaves no record, so the next land reads the same
                # first timeout, not a second
                label(number, DEQUEUED, add=False)
                comment(
                    number,
                    fold(
                        TIMED_OUT_ONCE,
                        f"{timeout}: queued again.",
                        f"Head: {head}\nRun: {said_span(budget)}",
                    ),
                )
                print(
                    f"{where}: Mergify dequeued it at {head} on a timeout ({timeout}): queued again, not evicted"
                )
                return 0
            if ours:
                mergify_evict(pull, labels)
                return 1
            label(number, DEQUEUED, add=False)
        queued = QUEUED in labels and ours
        if not queued:
            refused = unwritten(head, LAND, MERGIFY_QUEUED.format(head))
            if QUEUED not in labels:
                label(number, QUEUED, add=True)
            if refused is not None:
                handed_by_record(pull)
    except subprocess.CalledProcessError as exc:
        print(f"{where}: GitHub refused its hand-over to Mergify at {head}: {stderr_of(exc)}")
        return 1
    try:
        requeue(pull)
    except subprocess.CalledProcessError as exc:
        print(
            f"{where} is handed over to Mergify at {head}, but GitHub refused the `{REQUEUE}` command it needs"
            f" after its eviction there, which the next `land {number}` posts: {stderr_of(exc)}"
        )
        return 1
    print(
        f"{where} is queued for Mergify at {head}"
        if queued
        else f"{where} queued for Mergify at {head}"
    )
    return 0


# the first line of the hand-over record a cloud land comments when the `land` status is refused (#1816); the
# workflow `.github/workflows/land-status.yml` reads it and posts the `land` status on its `Head:`
HANDED = "Queued for Mergify"


def handed_by_record(pull: dict[str, Any]) -> None:
    """The hand-over where the `land` status can't be written (unwritten): a HANDED record naming the head, once per
    head, which the repo's land-status workflow turns into the `land` success on that sha while it is still the PR's
    head and the PR carries QUEUED, so `.mergify.yml` reads the same status and a push still leaves the queue. Once per
    head and hand-over: a land after an eviction at that head (QUEUE_EVICTED) posts a new record."""
    where, head = f"PR #{pull['number']}", pull["headRefOid"]
    # a record still stands until an eviction at the same head follows it: after one (a red batch, a conflict), the
    # head's `land` status may be the host's `error`, so a re-land posts a new record for the workflow to answer
    standing = False
    for note in notes(pull):
        first = machine(note["body"]).strip().splitlines()[:1]
        if fields(note["body"]).get("head", "").lower() != head.lower():
            continue
        if first == [HANDED]:
            standing = True
        elif first in ([QUEUE_EVICTED], [TIMED_OUT_TWICE]):
            standing = False
    if standing:
        print(
            f"{where}: its hand-over record for {head} is already on the PR; with no `land` status on {head} yet,"
            " re-run the land-status workflow's run for that comment, or `land` from a host"
        )
        return
    comment(
        pull["number"],
        fold(
            HANDED,
            f"Queued for Mergify at {head[:7]}: the land-status workflow posts the `land` status on it.",
            f"Head: {head}",
        ),
    )
    print(
        f"{where}: the hand-over record names {head}, for the land-status workflow to post `land` on"
    )


# the command that takes a PR Mergify dequeued (a red batch it found the PR's, a timeout) back into its queue: the
# `queue` action land's `.mergify.yml` runs does not re-embark such a PR at an unchanged head, and Mergify's `requeue`
# command is deprecated for this one (docs.mergify.com, merge-queue/lifecycle and commands/queue)
REQUEUE = "@mergifyio queue"


def requeue(pull: dict[str, Any]) -> None:
    """After a hand-over at a head land evicted from Mergify's queue before (evicted_at): REQUEUE, once per hand-over,
    so Mergify takes the head back once it matches its queue conditions; a hand-over whose command already follows
    that eviction posts none, and a head never evicted needs none."""
    head = pull["headRefOid"]
    evicted = evicted_at(pull, head)
    if evicted is None:
        return
    for note in notes(pull):
        if (
            note["body"].strip().splitlines()[:1] == [REQUEUE]
            and (note.get("createdAt") or "") >= evicted
        ):
            return
    comment(pull["number"], REQUEUE + "\n")
    print(f"PR #{pull['number']}: asked Mergify to queue {head} again, which it evicted before")


def mergify_evict(pull: dict[str, Any], labels: set[str]) -> None:
    """A PR Mergify dequeued at the head land queued: `land` `error` on that head first, so Mergify can't take it in
    again, both labels off, then back to draft with an `Evicted from the merge queue` comment (evicted) naming Mergify's
    reason where its check run on the head gives one (mergify_reason). Mergify already retried a red batch once
    (`max_checks_retries: 1`), so its dequeue is the second red and land keeps no count."""
    head, number = pull["headRefOid"], pull["number"]
    # refused (unwritten), the label coming off is what leaves the queue, since the land-status workflow posts nothing
    # without it
    unwritten(head, LAND, "Evicted from the Mergify merge queue", "error")
    for name in (QUEUED, DEQUEUED):
        if name in labels:
            label(number, name, add=False)
    why, tail = mergify_reason(head)
    evicted(
        pull, QUEUE_EVICTED, f"was dequeued by Mergify at {head}", why, tail, "Mergify's check run"
    )


# a Mergify queue check run's text for a dequeue on its `checks_timeout`, not on a red check (mergify_timeout)
TIMEOUT_TEXT = re.compile(r"\btim(?:ed|e)[ -]?out\b|\btimeout\b", re.IGNORECASE)


def mergify_timeout(head: str) -> tuple[str, str] | None:
    """Mergify's queue check run on `head`, when it says the dequeue was a timeout (TIMEOUT_TEXT in its title or
    summary): its name and title, and when it completed ("" where GitHub gives no time); None for any other dequeue,
    or a head GitHub can't read."""
    try:
        checks = commit_checks(head)
    except subprocess.CalledProcessError:
        return None
    for name, run in checks.items():
        if (
            re.search(r"mergify|queue", name, re.IGNORECASE)
            and run["conclusion"] not in ("SUCCESS", "NEUTRAL", "")
            and TIMEOUT_TEXT.search(f"{run.get('title', '')}\n{run.get('summary', '')}")
        ):
            title = run.get("title") or run["conclusion"].lower()
            return f"`{name}`: {title}", run.get("completed_at") or ""
    return None


# the records a timeout in Mergify's queue leaves at a head (#2308): the first, with the requeue it makes, and the
# second, which stops requeueing and names the `timeouts.run` it timed out under
TIMED_OUT_ONCE = "Timed out in the merge queue"
TIMED_OUT_TWICE = "Timed out twice in the merge queue"


def timeout_stop_line(where: str, head: str, budget: int) -> str:
    """The one line of a PR that timed out twice at one head: `timeouts.run`, the run's length, and what a person does."""
    return (
        f"{where} timed out twice in the merge queue at {head}, each run over `timeouts.run` ({said_span(budget)}): it"
        " stays out of the queue until the budget is raised (`timeouts.run`) or the suite sharded"
    )


def timeout_notes(pull: dict[str, Any], head: str, title: str, budget: int) -> list[dict[str, Any]]:
    """The PR's comments headed `title` that name `head` and the `timeouts.run` of `budget`, posted since it was last
    marked ready, so a ready-again starts the count anew, as the GitHub queue's removals do (queue_removals)."""
    ready = max(
        (
            event.get("createdAt") or ""
            for event in (pull.get("timelineItems") or {}).get("nodes") or []
            if event.get("__typename") == "ReadyForReviewEvent"
        ),
        default="",
    )
    return [
        note
        for note in notes(pull)
        if machine(note["body"]).strip().splitlines()[:1] == [title]
        and fields(note["body"]).get("head", "").lower() == head.lower()
        and fields(note["body"]).get("run") == said_span(budget)
        and (note.get("createdAt") or "") > ready
    ]


def timeout_stop_lifted_at(pull: dict[str, Any], head: str, budget: int) -> str:
    """When a `Timed out twice` record of `head` was left under another `timeouts.run` than `budget`, the newest such
    record's time, else "": the removals before it were timed out under a budget a person has since changed, so
    queue_step counts only those after it (#2308)."""
    return max(
        (
            note.get("createdAt") or ""
            for note in notes(pull)
            if machine(note["body"]).strip().splitlines()[:1] == [TIMED_OUT_TWICE]
            and fields(note["body"]).get("head", "").lower() == head.lower()
            and fields(note["body"]).get("run") != said_span(budget)
        ),
        default="",
    )


def timeout_recorded_at(pull: dict[str, Any], head: str) -> str:
    """When land last recorded a timeout at `head` (a `Timed out` or `Timed out twice` comment, whatever its budget and
    ready), else "": a timed-out queue check that completed before it is a dequeue's older one."""
    return max(
        (
            note.get("createdAt") or ""
            for title in (TIMED_OUT_ONCE, TIMED_OUT_TWICE)
            for note in notes(pull)
            if machine(note["body"]).strip().splitlines()[:1] == [title]
            and fields(note["body"]).get("head", "").lower() == head.lower()
        ),
        default="",
    )


def timed_out_twice(pull: dict[str, Any], budget: int) -> bool:
    """Whether land stopped the PR's head after its second timeout under this `timeouts.run`: raising the budget lifts
    the stop, since the run it timed out under is gone."""
    return bool(timeout_notes(pull, pull["headRefOid"], TIMED_OUT_TWICE, budget))


def mergify_reason(head: str) -> tuple[str, list[str]]:
    """Why Mergify dequeued `head`, from its queue check run there (`Mergify Merge Queue`, older configurations a
    `Queue:` or `Rule: ... (queue)` one) when it isn't green: its name, title and link; else that Mergify gave none."""
    try:
        checks = commit_checks(head)
    except subprocess.CalledProcessError:
        checks = {}
    for name, run in checks.items():
        if re.search(r"mergify|queue", name, re.IGNORECASE) and run["conclusion"] not in (
            "SUCCESS",
            "NEUTRAL",
            "",
        ):
            title = run.get("title") or run["conclusion"].lower()
            return (
                f"Mergify took it out of its merge queue at {head} (`{name}`: {title}, {run['url']})",
                [f"{name}: {title}", run["url"]],
            )
    return f"Mergify took it out of its merge queue at {head}, naming no reason on the head", []


def stderr_of(exc: Exception) -> str:
    """A failed call's error on one line: a gh call's stderr, else the exception's text."""
    return " ".join((getattr(exc, "stderr", None) or str(exc)).split())


def queue_step(
    pull: dict[str, Any], base: str, method: str, loop: str | None, required: set[str]
) -> tuple[int, bool]:
    """One candidate in the GitHub merge queue: the exit it means for a named PR, and with exit 0 whether the queue
    holds it after the step (a wait goes on from that), with exit 3 whether a wait would clear it. In the queue: left
    alone, or,
    stuck (stuck), dequeued and enqueued again. Out of it, by the queue's last removal of its head (queue_removals):
    merged, done (exit 0); a conflict GitHub still reports (or one it reports with no removal), commented once per head and
    not enqueued; a red group, after a check that `base`'s tip is green (main_red), counted only when built on a green
    commit of `base` (own_reds), then commented once and enqueued again, or evicted on the second red of the head; any
    other removal, or none, enqueued."""
    where, head = f"PR #{pull['number']}", pull["headRefOid"]
    # the queue merges every entry its one way
    if merge_method(pull, loop) == "merge" and method != "MERGE":
        print(
            f"{where}'s head `{pull['headRefName']}` matches `land.merge_commit_heads`, but {base}'s merge queue"
            f" merges every entry with {method}, not a merge commit: set the queue's merge method to MERGE"
        )
        return 1, False
    if entry := pull.get("mergeQueueEntry"):
        if not stuck(entry):
            print(f"{where} is in the merge queue at {head}, position {entry.get('position')}")
            return 0, True
        dequeue_pull(pull["number"], pull["id"])
        print(
            f"{where}: its merge-queue entry waited {STUCK // 60} minutes for checks GitHub never started:"
            " dequeued, to enqueue it again"
        )
        return enqueue(pull, method)
    budget = timeouts(loop)["run"]
    lifted = timeout_stop_lifted_at(pull, head, budget)
    removals = [
        removal for removal in queue_removals(pull) if (removal.get("createdAt") or "") > lifted
    ]
    reason = removals[-1].get("reason") if removals else None
    # GitHub takes a PR out of the queue a moment before it reports the merge: it is merged, nothing to do (exit 0)
    if reason == QUEUE_MERGED:
        print(f"{where} left the merge queue merged at {head}")
        return 0, False
    # the queue tests an entry on top of those ahead of it, so its conflict may be with one of them, not with the base:
    # it holds only while GitHub reports one, and exits 3 (no wait) while GitHub works it out again after the base moved
    mergeable = pull.get("mergeable")
    if reason == MERGE_CONFLICT and mergeable not in ("CONFLICTING", "MERGEABLE"):
        print(f"GitHub is still working out whether {where} conflicts with {base}")
        return WAITING, True
    if mergeable == "CONFLICTING":
        print(f"{where} conflicts with {base}: merge `origin/{base}` in")
        conflict_note(pull, base)
        return 1, False
    if (
        removals
        and timed_out_removal(removals[-1], required)
        and timeouts_at_head(removals, required) >= 2
    ):
        # the second timeout of this head: the same suite would time out again, each round a full run (#2308)
        print(timeout_stop_line(where, head, budget))
        if not timeout_notes(pull, head, TIMED_OUT_TWICE, budget):
            # the record a changed `timeouts.run` is read against (timeout_stop_lifted_at), once per head and budget
            try:
                comment(
                    pull["number"],
                    fold(
                        TIMED_OUT_TWICE,
                        timeout_stop_line(where, head, budget),
                        f"Head: {head}\nRun: {said_span(budget)}",
                    ),
                )
            except subprocess.CalledProcessError as exc:
                print(f"{where}: its `{TIMED_OUT_TWICE}` record was refused: {stderr_of(exc)}")
        return WAITING, False
    if reason == FAILED_CHECKS:
        main_red(base, required)
        reds = own_reds(where, base, removals, required)
        if reds is None:
            return WAITING, True
        group = group_of(removals[-1])
        if not reds or reds[-1] != group:
            print(
                f"{where} was red in the merge queue on a red {base}: it counts for nothing, enqueued again"
            )
            return enqueue(pull, method)
        name, run, tail = failing_check(group, required)
        why = f"`{name}` failed on the merge queue's commit {group} (run {run})"
        if len(reds) > 1:
            evicted(
                pull,
                QUEUE_EVICTED,
                f"is red in the merge queue twice at {head}",
                why + ", its second red on this head",
                tail,
                "The failed job's last lines",
            )
            return 1, False
        print(
            f"{where} is red in the merge queue at {head} ({name}): once more, in case it was a flake"
        )
        if not any(
            note["body"].startswith(QUEUE_RED) and f"Group: {group}" in note["body"]
            for note in notes(pull)
        ):
            comment(
                pull["number"],
                fold(
                    QUEUE_RED,
                    f"{why}: enqueued once more, in case it was a flake. A second red on this head turns it back"
                    " to draft.",
                    f"Head: {head}\nGroup: {group}",
                )
                + "\n"
                + folded("The failed job's last lines", tail),
            )
    return enqueue(pull, method)


def stuck(entry: dict[str, Any]) -> bool:
    """Whether a merge-queue entry has waited STUCK seconds for checks with no check suite on its commit, timed from
    that commit, so an entry long `QUEUED` behind others isn't stuck the moment its group is built. ponytail: takes
    `headCommit` to be the group's commit, which only a live queue proves; were it the PR's head, nothing is stuck."""
    commit = entry.get("headCommit") or {}
    if entry.get("state") != "AWAITING_CHECKS" or not commit.get("committedDate"):
        return False
    suites = (commit.get("checkSuites") or {}).get("totalCount") or 0
    built = datetime.fromisoformat(commit["committedDate"].replace("Z", "+00:00")).timestamp()
    return not suites and time.time() - built >= STUCK


def queue_removals(pull: dict[str, Any]) -> list[dict[str, Any]]:
    """The merge queue's removals of the PR's current head, oldest first: those after its push, since GitHub drops an
    entry whose head moves, and after it last went ready, so a PR evicted and marked ready again starts its count anew.
    ponytail: a head whose push GitHub's activity no longer lists counts each removal since it last went ready; read
    the head commit's date if a long-lived head ever needs it."""
    ready = [
        event.get("createdAt") or ""
        for event in pull["timelineItems"]["nodes"]
        if event.get("__typename") == "ReadyForReviewEvent"
    ]
    since = max([head_pushed(pull) or "", *ready])
    return [removal for removal in queue_events(pull) if (removal.get("createdAt") or "") > since]


def queue_events(pull: dict[str, Any]) -> list[dict[str, Any]]:
    """Every removal of the PR from the merge queue GitHub lists, oldest first."""
    return [removal for removal in (pull.get("removals") or {}).get("nodes") or [] if removal]


def group_of(removal: dict[str, Any]) -> str:
    """The merge queue's commit a removal's group was built as."""
    return (removal.get("beforeCommit") or {}).get("oid") or "unknown"


def own_reds(
    where: str, base: str, removals: list[dict[str, Any]], required: set[str]
) -> list[str] | None:
    """The groups of the `failed_checks` removals that count against the PR: each built on a commit of `base` whose
    required checks are green, since one built on a red commit failed for `base`'s fault, and one GitHub can't place
    counts. A group whose required checks failed only by timing out (`TIMED_OUT`) counts for nothing, as a
    `checks_timed_out` removal does: a timeout is no test's failure (#2228). None while a group's base commit is still
    running them: whose fault it was isn't known yet."""
    reds = []
    for removal in removals:
        if removal.get("reason") != FAILED_CHECKS:
            continue
        group = group_of(removal)
        if timed_out_only(group, required):
            continue
        if under := built_on(base, group):
            red, running = required_state(under, required, MAIN_FAULT)
            if running:
                print(f"{where}: {base}'s checks on {under} are still running")
                return None
            if red:
                continue
        reds.append(group)
    return reds


def timed_out_removal(removal: dict[str, Any], required: set[str]) -> bool:
    """Whether a merge-queue removal was a timeout: `checks_timed_out`, or `failed_checks` whose required checks only
    timed out (timed_out_only)."""
    reason = removal.get("reason")
    return reason == "checks_timed_out" or (
        reason == FAILED_CHECKS and timed_out_only(group_of(removal), required)
    )


def timeouts_at_head(removals: list[dict[str, Any]], required: set[str]) -> int:
    """How many of the head's removals (queue_removals) were timeouts: the queue's own record, no state of land's."""
    return sum(timed_out_removal(removal, required) for removal in removals)


def timed_out_only(group: str, required: set[str]) -> bool:
    """Whether the required checks on the queue's commit `group` failed only by timing out: some `TIMED_OUT`, none of
    MAIN_FAULT's other conclusions. A group GitHub can't read is no timeout."""
    try:
        red, _ = required_state(group, required, MAIN_FAULT)
        timed, _ = required_state(group, required, {"TIMED_OUT"})
    except subprocess.CalledProcessError:
        return False
    return bool(red) and red == timed


def built_on(base: str, group: str) -> str | None:
    """The commit of `base` the merge queue built its commit `group` on (their merge base), None where GitHub can't
    compare them."""
    try:
        return merge_base(base, group) or None
    except subprocess.CalledProcessError:
        return None


# the conclusions that make a red group main's fault on the commit it was built on: a run cancelled there (by a newer
# push's cancel-in-progress, say), gone stale or waiting for approval failed nothing, and the queue's own run on it
# went green, or GitHub wouldn't have merged it
MAIN_FAULT = {"FAILURE", "TIMED_OUT", "STARTUP_FAILURE", "ERROR"}


def required_state(
    ref: str, required: set[str], red: set[str] = RED
) -> tuple[list[str], list[str]]:
    """The checks a base requires (every check, where it requires none) on `ref`: those whose conclusion is in `red`,
    and those still running."""
    mine = {
        name: run["conclusion"]
        for name, run in commit_checks(ref).items()
        if not required or name in required
    }
    return sorted(name for name, done in mine.items() if done in red), sorted(
        name for name, done in mine.items() if not done
    )


def enqueue(pull: dict[str, Any], method: str) -> tuple[int, bool]:
    """Put the PR in the GitHub merge queue (merging with `method`) pinned to the head the proofs judged: exit 0 once
    it is in (and True: the queue holds it), 1 when GitHub refuses, or when its head moved as it went in (the forge
    port's REST answer reads it again)."""
    where, head = f"PR #{pull['number']}", pull["headRefOid"]
    # before the enqueue, so no PR the queue merges lacks it: the bare land's trail follows it (queue_trail)
    label(pull["number"], QUEUED, add=True)
    try:
        position = enqueue_pull(pull["number"], pull["id"], head, method)
    except subprocess.CalledProcessError as exc:
        print(f"{where}: GitHub refused to enqueue it at {head}: {stderr_of(exc)}")
        return 1, False
    except Refused as exc:
        print(f"{where}: {exc}")
        return 1, False
    print(f"{where} enqueued at {head}, position {position}")
    return 0, True


def run_link(url: str) -> str:
    """An Actions job's link as its run's."""
    return re.sub(r"/job/\d+$", "", url)


def main_red(base: str, required: set[str]) -> None:
    """Refuse while a check `base` requires (every check, where it requires none) is red on its tip: every queue entry
    would fail on it, so no removal counts as red and nobody is evicted until it is fixed."""
    if red := required_state(base, required)[0]:
        raise Refused(
            f"{base} itself is red ({', '.join(f'`{name}`' for name in red)} on its tip): no merge-queue removal counts"
            " as red and nobody is evicted until it is fixed"
        )


def failing_check(group: str, required: set[str]) -> tuple[str, str, list[str]]:
    """The check that failed on the merge queue's commit `group` (a required one first), its run's link, and its failed
    job log's last LOG_TAIL lines (none where GitHub has no log)."""
    checks = commit_checks(group)
    red = sorted(name for name, run in checks.items() if run["conclusion"] in RED)
    name = next((name for name in red if name in required), red[0] if red else None)
    if name is None:
        return "a required check", "not found", []
    try:
        log = job_log(checks[name]["id"])
    except subprocess.CalledProcessError:
        log = ""
    return name, run_link(checks[name]["url"]), log.splitlines()[-LOG_TAIL:]


def queue_landed(pull: dict[str, Any]) -> None:
    """A PR the GitHub merge queue merged gets its `Landed` comment, from the first land that reads it merged: `Base:`
    its merge commit, `Tree:` that commit's tree, `Gate: merge queue` with the links of the runs on it, the queue's
    `merge_group` CI; and its worker sessions finished. Nothing for a PR it did not merge, or one already commented."""
    merged = [removal for removal in queue_events(pull) if removal.get("reason") == QUEUE_MERGED]
    queued = QUEUED in {each["name"] for each in pull["labels"]["nodes"]}
    if pull["state"] == "MERGED":
        breakglass_landed(pull)
    if not any(note["body"].startswith(f"{LANDED}\n") for note in notes(pull)):
        if merged:
            landed_comment(pull, merged[-1])
        # Mergify's merges leave no removal: a merged PR land queued for it (QUEUED) was merged by its queue
        elif queued and pull["state"] == "MERGED" and mergify(project()[1]):
            mergify_landed(pull)
    # off once the trail is written, or where there is none to write (merged outside the queue)
    if queued:
        label(pull["number"], QUEUED, add=False)
    land_ended(pull)


def landed_comment(pull: dict[str, Any], merged: dict[str, Any]) -> None:
    """queue_landed's `Landed` comment on the commit the queue's `merged` removal names, and the worker sessions."""
    commit = (merged.get("beforeCommit") or {}).get("oid")
    tree = commit_tree(commit)
    runs = sorted(
        {
            run_link(run["url"])
            for run in commit_checks(commit).values()
            if "/actions/runs/" in run["url"]
        }
    )
    comment(
        pull["number"],
        fold(
            LANDED,
            f"Merged by the merge queue as {commit[:7]}.",
            f"Base: {commit}\nTree: {tree}\nGate: merge queue, {', '.join(runs) or 'no run on the commit'}",
        ),
    )
    archive_workers(pull["number"])


def mergify_landed(pull: dict[str, Any]) -> None:
    """queue_landed's `Landed` comment on a PR the Mergify queue merged: `Base:` its merge commit, `Tree:` that
    commit's tree and `Gate: Mergify merge queue` with the links of the Actions runs on that commit; and the worker
    sessions. ponytail: the merge commit's runs, not those on Mergify's draft PR, which no REST read of the PR names."""
    found = merged_pull(pull["number"]) or {}
    commit = found.get("merge_commit_sha")
    if not commit:
        raise Refused(f"GitHub names no merge commit of PR #{pull['number']}")
    runs = sorted(
        {
            run_link(run["url"])
            for run in commit_checks(commit).values()
            if "/actions/runs/" in run["url"]
        }
    )
    comment(
        pull["number"],
        fold(
            LANDED,
            f"Merged by the Mergify merge queue as {commit[:7]}.",
            f"Base: {commit}\nTree: {commit_tree(commit)}\nGate: {MERGIFY_GATE}, {', '.join(runs) or 'no run on the commit'}",
        ),
    )
    archive_workers(pull["number"])


def merged_anyway(number: int, head: str, exc: subprocess.CalledProcessError) -> str:
    """The merge commit of PR #number when `gh pr merge` failed with exc but GitHub merged it at head all the same,
    since a transport error (a response cut off mid-stream) can come after the merge. Anything else is Refused with
    exc's error, and with the re-read's as well when re-reading the PR failed too."""
    why = " ".join(str(exc.stderr or exc).split())
    try:
        found = merged_pull(number)
    except (subprocess.CalledProcessError, ValueError) as again:
        reread = " ".join(str(getattr(again, "stderr", None) or again).split())
        raise Refused(f"{why}; re-reading the PR failed too: {reread}") from exc
    if found is None or (found.get("head") or {}).get("sha") != head:
        raise Refused(why) from exc
    return str(found.get("merge_commit_sha"))


def refuse_red(pull: dict[str, Any], base: str) -> None:
    """Refuse to merge a PR while a check `base` requires is red, pending or missing on its head, whether or not the owner
    approved a break-glass: land's merge, signed squash included, is never the bypass (#1722)."""
    required = required_checks(base) - OWN
    if required and (lines := ci(f"PR #{pull['number']}", pull, required)):
        raise Refused(
            f"PR #{pull['number']}: refusing to merge while a required check is not green: {'; '.join(lines)}"
            " (a break-glass PR is merged by the owner in the GitHub UI, never by land)"
        )


def merges(
    landed: list[tuple[dict[str, Any], str]],
    base: str,
    tip: str,
    loop: str | None,
    failed: set[int],
    waited: str = "",
    held: Sequence[str] | None = None,
) -> None:
    """Merge each PR through GitHub in the batch's order, pinned to its head (or, with loop.md's `land` section, as a
    signed squash of the tree it lands on the commit before it), stopping at the first one GitHub refuses or leaves
    open, or before the next once the heartbeat lost the lock (`held` emptied; None: no lock). No gate runs on the
    base's new tip. Each landed PR gets a `Landed` comment. Right before its merge call a PR's head gets the `land`
    status naming the nonce of the lock commit `held` ends with, which the base's ruleset can require, so only a land
    merges (#1289); a merge GitHub then refuses, or leaves open, turns it to an error."""
    local, done = signed(loop), []

    def owned() -> str | None:
        """The lock commit `held` ends with (None: no lock), read under `beating` so the heartbeat can't empty it
        between the check and the read; [] is a lock the heartbeat lost, which stops the batch. Checked again right
        before each merge call, since the steps before it reach GitHub too; a call already made can't be taken back."""
        with beating:
            if held is not None and not held:
                raise Refused(f"another land took the landing lock {lock_name(base)}")
            return held[-1] if held else None

    for pull, tree in landed:
        where, head, posted = f"PR #{pull['number']}", pull["headRefOid"], False
        try:
            refuse_red(pull, base)
            ours = owned()
            nonce = (
                git("log", "-1", "--format=%b", ours).rsplit("nonce ", 1)[-1].strip()
                if ours
                else ""
            )
            method = merge_method(pull, loop)
            if local:
                owned()
                tip = signed_squash(pull, local[1], (tip, tree))
                print(f"{where} lands as a {method}")
                print(f"{where} landed in {base} as {tip}, signed by a key in {local[1]}")
            else:
                # ponytail: no --delete-branch, which also deletes the local branch a worktree has checked out; the
                # repo's delete_branch_on_merge removes the remote one
                print(f"{where} lands as a {method}")
                # a cloud session's proxy refuses the status write (#1816) but passes the merge, so a refusal lands
                # it all the same, with no success to withdraw if the merge fails (#1917)
                posted = (
                    unwritten(
                        head, LAND, f"Landing under the lock {lock_name(base)}, nonce {nonce}"
                    )
                    is None
                )
                try:
                    owned()
                    port("forge").merge(pull["number"], method, head)
                except subprocess.CalledProcessError as exc:
                    merge = merged_anyway(pull["number"], head, exc)
                    print(
                        f"{where}: the merge call failed, but the PR is merged at {head} as {merge}"
                    )
                else:
                    # a merge queue the base's rules don't name (classic branch protection's) only queues it, to
                    # merge later on a tree no land saw
                    if (state := pull_status(pull["number"])["state"]) != "MERGED":
                        raise Refused(f"GitHub left it {state}, not merged")
                    print(f"{where} merged at {head}")
        except (subprocess.CalledProcessError, Refused) as exc:
            why = " ".join(str(getattr(exc, "stderr", None) or exc).split())
            print(
                f"{where}: the merge was refused, so it and every PR after it wait for the next land: {why}"
            )
            if posted:
                # GitHub keeps the newest status per context: an error replaces the success, so a later merge of
                # this head made without land is refused again
                try:
                    post_status(head, LAND, f"Not merged: {why}"[:140], "error")
                except subprocess.CalledProcessError as failure:
                    print(
                        f"{where}: its `land` success could not be withdrawn: {stderr_of(failure)}"
                    )
            failed.update(other["number"] for other, *_ in landed[len(done) :])
            break
        done.append((pull, tree))
    if not done:
        return
    git("fetch", "-q", "origin", f"+refs/heads/{base}:refs/remotes/origin/{base}")
    tip = git("rev-parse", f"refs/remotes/origin/{base}").strip()
    drop_records([pull["headRefOid"] for pull, _ in done])
    numbers = ", ".join(f"#{pull['number']}" for pull, _ in done)
    for pull, tree in done:
        comment(
            pull["number"],
            fold(
                LANDED,
                f"Merged as {tip[:7]}.",
                f"Base: {tip}\nTree: {tree}\nGate: {NO_GATE}\nBatch: {numbers}{waited}",
            ),
        )
        archive_workers(pull["number"])
        land_ended(pull)


def installed_scripts(skill: str) -> Path | None:
    """The scripts folder of `skill`, which another package ships, found by the host's install layout: a sibling
    skill folder (APM's one skills directory), a sibling plugin's (this repo, or a marketplace Claude Code loads in
    place), else the newest version of a plugin in Claude Code's cache (`<marketplace>/<plugin>/<version>/`) that
    no update has orphaned. None when none holds it."""
    # ponytail: three layouts, the ones APM and Claude Code use; a host with another adds its glob here.
    own = Path(__file__).resolve().parents[1]
    plugin = own.parent.parent
    cached = plugin.parent.parent.glob(f"[!.]*/[!.]*/skills/{skill}")
    for folder in [
        own.parent / skill,
        *sorted(plugin.parent.glob(f"[!.]*/skills/{skill}")),
        *sorted(cached, key=lambda path: path.stat().st_mtime, reverse=True),
    ]:
        if (folder / "SKILL.md").is_file() and not (folder.parents[1] / ".orphaned_at").exists():
            return folder / "scripts"
    return None


def agent_script() -> Path:
    """The omnigent-agents skill's omnigent_agent.py, run as a command, since skills install separately and neither
    may import the other. A host without the skill gets the sibling path, which does not exist: the caller's own
    `is_file` check refuses."""
    found = installed_scripts("omnigent-agents")
    return (
        found or Path(__file__).resolve().parents[2] / "omnigent-agents" / "scripts"
    ) / "omnigent_agent.py"


own_started: set[str] = (
    set()
)  # the `owner/name#N` refs own_prs has started, so a land --wait starts each once


def own_prs(pulls: Sequence[dict[str, Any]]) -> None:
    """Start, detached, one run of the omnigent-agents skill's `omnigent_agent.py own-pr <owner/name#N>...` for the
    PRs this land names, so the merge queue's dequeue of any reaches this session when it is a followed lead that did
    not start it. One run, one write of the label: runs per PR would each read it and the last would win. Best effort
    and never waited on: a host without the skill or without Omnigent, or a start that fails, is silent, since the land
    is not about the session's labels. Each ref is started once per process, however often the land polls."""
    script = agent_script()
    if not script.is_file():
        return
    refs = [
        ref
        for pull in pulls
        if (repo := (pull.get("baseRepository") or {}).get("nameWithOwner")) and pull.get("number")
        if (ref := f"{repo}#{pull['number']}") not in own_started
    ]
    if not refs:
        return
    try:
        subprocess.Popen(
            [sys.executable, str(script), "own-pr", *refs],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return
    own_started.update(refs)


def archive_workers(number: int) -> None:
    """Start, detached, the finish of the kept `--pr` worker sessions of PR #number, which merged:
    the omnigent-agents skill's `omnigent_agent.py reap --merged --pr <n>`. It is not waited on, so a slow Omnigent or
    GitHub never holds the land, and its outcome goes to its own log, named in the line printed. A checkout without
    that skill, or a start that fails, is a printed line only: the PR has landed, and a session left open is the
    next `reap`'s."""
    script = agent_script()
    if not script.is_file():
        print(f"PR #{number}: no worker sessions finished: {script.name} is not installed")
        return
    try:
        with tempfile.NamedTemporaryFile(
            "w", prefix=f"reap-pr{number}-", suffix=".log", delete=False
        ) as log:
            subprocess.Popen(
                [sys.executable, str(script), "reap", "--merged", "--pr", str(number)],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
    except OSError as exc:
        print(f"PR #{number}: worker sessions not finished: {exc}")
        return
    print(f"PR #{number}: finishing its worker sessions in the background, log {log.name}")


def approve(point: str, number: int, seen: str | None = None, by: str = "owner") -> int:
    """Record the owner's approval of the issue's current spec or plan, on their word: the record, the label, and
    `needs-owner` removed. Every earlier record of the point is superseded and minimized. With `seen`, the version
    the owner's label was checked against (adopted), an edit since is refused: the label never covered it. With `by`
    `delegate`, the delegated approver's, under the owner's delegation: refused unless the policy on origin's default
    branch sets `loop.delegate: all`, its record opening with VOICED's line so a reader tells it from the owner's; the
    gate counts it as delegated() says. `loop.delegate_spec: false` there refuses it too, keeping the spec and plan
    the owner's."""
    if by == DELEGATE:
        loop = project()[1]
        if delegation(loop) != "all":
            raise Refused(
                f"approve {point} --by delegate needs `loop.delegate: all` in the policy on origin's default branch:"
                f" without it the {point} is the owner's (`--by owner`)"
            )
        if not delegate_spec(loop):
            raise Refused(
                f"approve {point} --by delegate is refused: the policy on origin's default branch sets"
                f" `loop.delegate_spec: false`, so the {point} is the owner's (`--by owner`); the delegate's go on a"
                " merge still counts"
            )
    issue = issue_node(number)
    holder = spec_holder(issue) if point == "spec" else headed(issue, "## Plan")
    if holder is None:
        raise Refused(f"#{number} has no `## Plan` comment to approve")
    value = texted(holder)
    if seen is not None and value != seen:
        raise Refused(
            f"#{number}: the {point} was edited after the owner's `approved:{point}`: the owner adds it again"
        )
    record = f"{point.capitalize()}: {value}\n"
    earlier = [
        (note, found) for note in notes(issue) if (found := parsed(note["body"], point)) is not None
    ]
    if any(
        found.get("by") == "owner" and found.get(point) not in versions(holder)
        for _, found in earlier
    ):
        record += CHANGED[point]
    if by == DELEGATE:
        comment(
            number,
            fold(
                VOICED[DELEGATE],
                f"The delegate approved the {point}.",
                f"Approved: {point}\nBy: {DELEGATE}\n{record}",
            ),
        )
    else:
        comment(
            number,
            fold(f"Approved: {point}", f"The owner approved the {point}.", f"By: owner\n{record}"),
        )
    for note, _ in earlier:
        if not note.get("isMinimized") and note.get("viewerCanMinimize"):
            minimize(note["id"])
    label(number, f"approved:{point}", add=True)
    # the issue's `needs-owner` is its spec's or plan's hold, which problems() reads whatever the records say, so the
    # delegate's approval lifts it too; a critical path still waits for the owner's record (delegated)
    label(number, NEEDS_OWNER, add=False)
    if by == DELEGATE:
        print(f"#{number}: the delegate's approval of the {point} is recorded{DELEGATED}")
    return 0


def delegate(number: int) -> int:
    """The delegated approver's `approved:merge` on a PR into a feature branch, under the owner's standing rule: the
    label, then the record that names it the delegate's. Into the default branch, where a label is no go, it is
    refused: there the go is pinned to a head (go). So is a PR whose rating asks the owner's own go on the head
    (owners_only; by default a path `risk.critical_paths` names)."""
    pull = pull_request(number)
    with policy_from(pull["baseRefName"]):
        loop = project()[1]
    if sole := owners_only(pull, loop):
        raise Refused(
            f"PR #{number}: `{MERGE_LABEL}` is no go here, since {sole}: it waits for the owner's go on its head"
        )
    if default(pull):
        raise Refused(
            f"PR #{number} targets {pull['baseRefName']}, the default branch, where a label is no go: the head-pinned"
            f" `approve merge {number} --head <sha> --by delegate` gives the delegate's go there with `loop.delegate:"
            " all`, and the owner's go always covers it"
        )
    # the label first, so the record is never older than the label it names
    label(number, MERGE_LABEL, add=True)
    try:
        comment(
            number,
            fold("Approved: merge", "The delegate approved the merge.", f"By: {DELEGATE}"),
        )
    except subprocess.CalledProcessError:
        # a label with no record names no one: take it off again
        label(number, MERGE_LABEL, add=False)
        raise
    return 0


# a report's finding heading: `### <id> (<severity>) <path>:<line>: <title>`, the line a number or a range `<n>-<m>`
FINDING = re.compile(
    r"###[ \t]+(\S+)[ \t]+\((blocker|major|minor)\)[ \t]+`?([^\s`]+?):(\d+(?:-\d+)?)`?:?[ \t]+(.+?)[ \t]*",
    re.IGNORECASE,
)
# a `### ` heading with a severity in parentheses is meant as a finding; any other is a sub-heading, and a finding written
# without one is caught by verdict()'s count check
SEVERITY = re.compile(r"\((blocker|major|minor)\)", re.IGNORECASE)
# the verdict's counts, which every finding heading must add up to
COUNTED = re.compile(r"VERDICT:[ \t]*(\d+) blockers?,[ \t]*(\d+) majors?,[ \t]*(\d+) minors?\b.*")


def findings(text: str, report: Path) -> list[tuple[str, str, str, str, str, str]]:
    """Each finding of a report as (id, severity, path, line, title, body): its body up to the next heading of
    levels 1 to 3 or the closing `Head:`/`VERDICT:`/`SATISFIED:` lines, outside a code fence. A `### ` heading with
    a `(<severity>)` that doesn't read as a finding is refused, since the review would leave it out without a word;
    any other is a sub-heading, which posts nothing."""
    found: list[tuple[str, str, str, str, str, list[str]]] = []
    body: list[str] | None = None  # the open finding's lines
    fenced = False
    for line in text.splitlines():
        if re.match(r"[ \t>]*(```|~~~)", line):
            fenced = not fenced
        elif not fenced and re.match(r"#{1,3} |(Head|VERDICT|SATISFIED):", line):
            body = None
            if line.startswith("### ") and SEVERITY.search(line):
                heading = FINDING.fullmatch(line.rstrip())
                if not heading:
                    raise Refused(
                        f"{report}: can't read the finding heading `{line.strip()}`: write it "
                        "`### <id> (<blocker|major|minor>) <path>:<line>: <title>`"
                    )
                name, severity, path, at, title = heading.groups()
                body = []
                found.append((name, severity.lower(), path, at, title, body))
            continue
        if body is not None:
            body.append(line)
    return [(*finding[:5], "\n".join(finding[5]).strip()) for finding in found]


# a manual acceptance line the verifier took on the worker's evidence: `Accepted on evidence: <line> — <proof log>`
ACCEPTED = "Accepted on evidence:"


def criteria(text: str) -> str:
    """A report's `## Criteria` section, the criteria the verifier wrote from the spec before reading the worker's
    evidence (the verifier role, Criteria first), up to the next heading of levels 1 or 2 or the closing lines, outside a
    code fence; '' when it has none or the section is empty."""
    section: list[str] | None = None
    fenced = False
    for line in text.splitlines():
        fence = re.match(r"[ \t>]*(```|~~~)", line)
        if not fenced and not fence and re.match(r"#{1,2} |(Head|VERDICT|SATISFIED):", line):
            if section is not None:
                break
            if line.rstrip() == "## Criteria":
                section = []
            continue
        if fence:
            fenced = not fenced
        if section is not None:
            section.append(line)
    return "\n".join(section or []).strip()


def accepted(lines: list[str], report: Path) -> list[tuple[str, str]]:
    """Each `Accepted on evidence: <line> — <proof log>` line of a report as (line, proof log), refused when one
    names no proof log, since an acceptance taken on evidence nobody can find is no acceptance."""
    found = []
    fenced = False
    for line in lines:
        if line.startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if fenced:  # a quoted report line is no acceptance
            continue
        # a `Manual:` line that takes itself on evidence inline: the high-risk refusal and the posted review would
        # never see it; prose, a quote or a fenced block that only mentions the phrase is no acceptance
        if not line.startswith(ACCEPTED) and re.search(
            rf"Manual:.*{re.escape(ACCEPTED)}", re.sub(r"`[^`]*`|\*", "", line)
        ):
            raise Refused(
                f"{report}: `{ACCEPTED}` inside `{line[:60]}`: write it as a line of its own, "
                f"`{ACCEPTED} <line> — <proof log>`"
            )
        if not line.startswith(ACCEPTED):
            continue
        said = re.fullmatch(r"(\S.*)[ \t]+[—–][ \t]+(\S.*)", line[len(ACCEPTED) :].strip())
        if not said:
            raise Refused(
                f"{report}: can't read `{line}`: write `{ACCEPTED} <line> — <proof log>`, naming the worker's "
                "proof log it rests on"
            )
        found.append((said[1], said[2]))
    return found


def parse_report(
    report: Path,
) -> tuple[str, str, list[tuple[str, str, str, str, str, str]], list[str], str]:
    """A verifier report as `verdict` posts it: its review heading, the head it reviewed, its findings, its closing
    `VERDICT:`, `SATISFIED:`, `Risk:` and any `Logic change:`, `Scope:` and `Rerun:` lines (the last two read after
    the `Head:` line), and the qualifier its `Verifier:` line has after the pass number ('' for
    none). Refused, naming the fix, when `verdict` couldn't post it whole."""
    text = read(report)
    lines = [line.strip() for line in text.splitlines()]
    verdicts = [line for line in lines if line.startswith("VERDICT:")]
    satisfied = [line for line in lines if line.startswith("SATISFIED:")]
    if not satisfied or not verdicts:
        raise Refused(f"{report} has no `VERDICT:` or `SATISFIED:` line: the report is unfinished")
    judgments = [line for line in lines if re.match(r"Risk:[ \t]*\S", line)]
    if not judgments:
        raise Refused(f"{report} has no `Risk: low|high — <reason>` line: the report is unfinished")
    # the head the verifier reviewed, which a push since the review may have moved the PR's head past
    heads = [
        found[1] for line in lines if (found := re.fullmatch(r"Head:[ \t]*([0-9a-fA-F]{40})", line))
    ]
    if not heads:
        raise Refused(f"{report} has no `Head: <sha>` line naming the full commit it reviewed")
    passes = [
        found
        for line in lines
        # free text after the pass number is a qualifier the review's first line carries
        if (
            found := re.fullmatch(
                r"Verifier:[ \t]*(.+?),[ \t]*pass[ \t]+(\d+)\b[ \t]*[:,;]?[ \t]*(.*)", line
            )
        )
    ]
    if not passes:
        raise Refused(f"{report} has no `Verifier: <family>, pass <n>` line")
    counted = COUNTED.fullmatch(verdicts[-1])
    if not counted:
        raise Refused(f"{report}: write the verdict `VERDICT: <n> blocker, <n> major, <n> minor`")
    # a finding whose heading doesn't read as one would be left off the review without a word
    found = findings(text, report)
    tally = tuple(sum(f[1] == kind for f in found) for kind in ("blocker", "major", "minor"))
    if tally != tuple(map(int, counted.groups())):
        raise Refused(
            f"{report}: `{verdicts[-1]}` counts {sum(map(int, counted.groups()))} findings, but {len(found)} "
            f"headings read as one ({tally[0]} blocker, {tally[1]} major, {tally[2]} minor): write each "
            "`### <id> (<blocker|major|minor>) <path>:<line>: <title>`"
        )
    # the criteria come from the spec before the worker's evidence, so its framing can't narrow the review
    if not criteria(text):
        raise Refused(
            f"{report} has no `## Criteria` section: write, from the spec alone and before reading the worker's "
            "report, the criteria the change must meet, each mapped to a Before merge line and its check "
            "(the verifier role, Criteria first)"
        )
    # a manual line taken on the worker's evidence holds only on a low-risk change; any word but `low` reads as high,
    # as the gate reads it (risk)
    if not re.match(r"low\b", judgments[-1].split(":", 1)[1].strip(), re.IGNORECASE) and (
        taken := [line for line in lines if line.startswith(ACCEPTED)]
    ):
        raise Refused(
            f"{report}: `{judgments[-1]}` with `{taken[0]}`: under a high risk a `Manual:` line is probed by hand, "
            "never accepted on the worker's evidence: run it, or ask for the run in `<report>.question` "
            "(the verifier role, Manual lines)"
        )
    accepted(lines, report)
    # a run not made can't back a satisfied verdict: that ends in a question for the lead's proof run
    if satisfied[-1].lower().split()[1:2] == ["yes"] and any(
        re.match(r"Not run:(?!\s*none\.?\s*$)", line, re.IGNORECASE) for line in lines
    ):
        raise Refused(
            f"{report}: `{satisfied[-1]}` lists a `Not run:` line, so a run it relies on was never made: "
            "end the turn in `<report>.question` (the verifier role, A run the sandbox refuses)"
        )
    # an unsatisfied pass with nothing found spends a pass on a run the verifier couldn't make: that ends in a question
    if (
        not any(tally)
        and satisfied[-1].lower().split()[1:2] != ["yes"]
        and not any(re.match(r"Not run:.*\bowner action:\s*\S", line) for line in lines)
    ):
        raise Refused(
            f"{report}: `{satisfied[-1]}` with no finding spends a pass on a run the sandbox refused: "
            "end the turn in `<report>.question` (the verifier role, A run the sandbox refuses), or name what "
            "was not run on a `Not run: <step>, owner action: <what>` line"
        )
    family, number, qualifier = passes[-1].groups()
    logic = [line for line in lines if line.startswith(f"{LOGIC}:")]
    # only after the `Head:` line, with the other closing lines: a criteria line saying `Scope: full` is no scope
    last_head = max(
        i for i, line in enumerate(lines) if re.fullmatch(r"Head:[ \t]*[0-9a-fA-F]{40}", line)
    )
    scope = [line for line in lines[last_head:] if re.match(r"Scope:[ \t]*\S", line)]
    # the rerun a fresh session the verifier started pushed, which the gate reads from the posted review (passes)
    reruns_named = [line for line in lines[last_head:] if line.startswith(f"{RERUN_LINE}:")]
    if reruns_named and not re.fullmatch(
        rf"{RERUN_LINE}:[ \t]*[0-9a-fA-F]{{40}}\b.*", reruns_named[-1]
    ):
        raise Refused(
            f"{report}: `{reruns_named[-1]}`: write `{RERUN_LINE}: <commit>`, the full commit of the record branch the"
            " rerun pushed"
        )
    closing = [
        verdicts[-1],
        satisfied[-1],
        judgments[-1],
        *logic[-1:],
        *scope[-1:],
        *reruns_named[-1:],
    ]
    heading = f"Verifier ({family}), pass {number}"
    return heading, heads[-1].lower(), found, closing, qualifier


def report_check(report: Path) -> int:
    """Check a verifier report the way `verdict` reads it, posting nothing, so the verifier fixes it before replying;
    a warning on stderr where it would leave a high-risk head without the rerun it needs (unnamed_rerun)."""
    closing = parse_report(report)[3]
    if warning := unnamed_rerun(report, closing):
        print(warning, file=sys.stderr)
    print(f"{report} reads as a verdict")
    return 0


def unnamed_rerun(report: Path, closing: list[str]) -> str | None:
    """A warning, or None: the report rates the change high (any `Risk:` word but `low`), the policy asks a rerun at
    `high` and has no CI to make it (`loop.ci: false`, where the verifier starts the rerun), and the report names no
    `Rerun:` record, so `check merge` would refuse the head. A warning, not a refusal: a label or path may already
    rate the change high whatever the verifier says, and the gate itself refuses the merge. Outside a checkout, or
    with a policy that can't be read, None."""
    if re.match(r"Risk:[ \t]*low\b", closing[2], re.IGNORECASE) or any(
        line.startswith(f"{RERUN_LINE}:") for line in closing
    ):
        return None
    try:
        loop = project()[1]
    except (Refused, OSError, subprocess.CalledProcessError):
        return None
    if not no_ci(loop) or not levels(loop)["high"].get("rerun"):
        return None
    return (
        f"warning: {report} rates the change high and names no `{RERUN_LINE}: <commit>`, and this repo has no CI to"
        " rerun its selection: `check merge` will refuse the head until a verifier pass on it names the record a fresh"
        " session's `local-ci --record --rerun` pushed"
    )


def report_facts(report: Path) -> dict[str, Any]:
    """A verifier report's facts, as `verdict` reads them (parse_report, which refuses an unfinished one): the
    verifier's family, pass and qualifier, the head it reviewed, the tally, whether it is satisfied, the risk and its
    reason, the `Logic change:` and `Scope:` lines, each finding, the `## Criteria` section and each `Accepted on evidence:` line, so
    a flow engine never parses the prose."""
    heading, head, found, closing, qualifier = parse_report(report)
    family, number = PASS.fullmatch(heading).groups()  # type: ignore[union-attr]
    counted = COUNTED.fullmatch(closing[0])
    assert counted  # parse_report refused a report without one
    risk = re.match(r"Risk:[ \t]*(\S+)[ \t]*[—–-]*[ \t]*(.*)", closing[2])
    assert risk  # the same
    logic = [line.partition(":")[2].strip() for line in closing[3:] if line.startswith(f"{LOGIC}:")]
    scope = [line.partition(":")[2].strip() for line in closing[3:] if line.startswith("Scope:")]
    rerun = [
        line.partition(":")[2].split()[0].lower()
        for line in closing[3:]
        if line.startswith(f"{RERUN_LINE}:")
    ]
    text = read(report)
    return {
        "criteria": criteria(text),
        "accepted_on_evidence": [
            {"line": line, "proof": proof}
            for line, proof in accepted([line.strip() for line in text.splitlines()], report)
        ],
        "verifier": {"family": family, "pass": int(number), "qualifier": qualifier},
        "head": head,
        "verdict": dict(
            zip(("blocker", "major", "minor"), map(int, counted.groups()), strict=True)
        ),
        "satisfied": closing[1].lower().split()[1:2] == ["yes"],
        "risk": {"level": risk[1].lower(), "reason": risk[2].strip()},
        "logic_change": logic[0] if logic else None,
        "scope": scope[0] if scope else None,
        "rerun": rerun[0] if rerun else None,
        "findings": [
            dict(zip(("id", "severity", "path", "line", "title", "body"), finding, strict=True))
            for finding in found
        ],
    }


def report_json(report: Path) -> int:
    """`report --json`: the report's facts (report_facts) as one JSON object; an unfinished report is refused as
    `report` refuses it, with its reason and exit 1."""
    print(json.dumps(report_facts(report), indent=2))
    return 0


def verdict(pr: int, report: Path) -> int:
    """Post a verifier report as a PR review on the commit it reviewed: one inline comment per finding, at the nearest
    line of the diff when its line is outside it, in the review's body when its file is."""
    heading, head, found, closing, qualifier = parse_report(report)
    denied(report.read_text())  # before any read of GitHub, not only at the post
    if warning := unnamed_rerun(report, closing):
        print(warning, file=sys.stderr)
    diff = diff_lines(pull_request(pr)["baseRefOid"], head)
    comments, outside = [], []
    for name, severity, path, line, title, body in found:
        said = f"**{heading} · {name}** ({severity}): {title}"
        if not diff.get(path):
            outside.append(f"- {said}, at `{path}:{line}`, a file outside the diff\n\n{body}")
            continue
        last = int(line.rpartition("-")[2])  # a range anchors at its last line
        at = min(diff[path], key=lambda candidate: abs(candidate - last))
        moved = (
            f"At `{path}:{line}`, outside the diff: placed at the nearest diff line.\n\n"
            if at != last
            else ""
        )
        comments.append(
            {"path": path, "line": at, "side": "RIGHT", "body": f"{said}\n\n{moved}{body}"}
        )
    first = f"{heading} on {head[:7]}" + (f": {qualifier}" if qualifier else "") + "."
    text = read(report)
    # the criteria first, as the verifier wrote them before the worker's evidence, and each line taken on that evidence
    taken = [line.strip() for line in text.splitlines() if line.strip().startswith(ACCEPTED)]
    # the verdict lines stay in view, as the gate reads them (passes); the criteria and the rest fold
    summary = fold(
        first,
        "\n".join(closing),
        "\n\n".join(
            [f"## Criteria\n\n{criteria(text)}", *outside, *(["\n".join(taken)] if taken else [])]
        ),
    )
    post_review(pr, {"commit_id": head, "event": "COMMENT", "body": summary, "comments": comments})
    hand_over(pr)
    return 0


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *SIGNING, *args], check=True, capture_output=True, text=True
    ).stdout


LOCAL_CI = ["mise", "run", "check:all"]
# a status description's limit on GitHub, which holds the tree, the commit and the gate command (described)
DESCRIBED = 140


def gate(passed: list[str] | tuple[str, ...] = ()) -> tuple[str, list[str], dict[str, str]]:
    """loop.md's `gate` under `loop` (default LOCAL_CI): the command as written, its words, and the variables it sets
    in front of them (`GATE_UI_TESTS=0 mise run check:all`). The command alone is the gate: it runs with every name
    `local_ci_env` lists cleared but those it sets, so no host's ambient env changes what it ran. With `passed`, a
    head proof's names (head_proof), each one this process's environment sets is set to that value, over the
    command's own assignment of it, and the command returned names it, so its record and status say so. With
    `gate_steps` (gate_plan), the gate as people read it: `<name>: <command>` for each step, joined by `; `, no words
    (each step runs its own), and the variables every step sets. Under narrowed_to a base in narrowed mode, the
    narrowed gate (narrowed_gate) instead."""
    if (narrow := narrowed_gate(passed)) is not None:
        return narrow
    loop = settings(project()[1])
    if not loop.get("gate_steps"):
        return command_line(loop.get("gate") or shlex.join(LOCAL_CI), "gate", passed)
    plan = gate_plan(passed)
    return (
        "; ".join(f"{step['name']}: {step['command']}" for step in plan),
        [],
        {name: value for step in plan for name, value in step["sets"].items()},
    )


# the name the one step of a gate without `gate_steps` is shown by; it is that gate by its `legacy` flag, never by this
# name, which a project's `gate_steps` may give a step too
WHOLE = "gate"


def gate_plan(
    passed: list[str] | tuple[str, ...] = (), every: bool = False
) -> list[dict[str, Any]]:
    """The gate's steps, in order, each `name`, `command` (as gate() reads one, with `passed`), `words`, `sets` and
    `where` (`any`, or `host`: never run in a cloud session): the base's policy's `gate_steps` (project(), so a PR's own
    edit of its gate is not the one it is gated by), else the one `gate` command as
    the step WHOLE, `any`, flagged `legacy`, which records, publishes and prints as the one gate always did (a plan
    from `gate_steps` is stepped whatever its names or length: its steps are not `legacy`). Each step's pass is recorded and reused by itself, keyed by its tree and command (recorded),
    so a step that passed on a tree anywhere is not run again; a project without `gate_steps` keeps one record. A
    policy setting both is refused, since two sources of the gate would drift. A step `on_demand` marks (#2295) is in
    the plan only with `every` (a lookup of its pass, or run_steps, which says it skipped it) or when ASKED names it:
    the gate's pass never needs it. With ASKED (`--step`), the plan is the steps it names alone. A `gate_steps` whose
    every step is on demand is refused, since its gate would have no step and pass every head with no run."""
    loop = settings(project()[1])
    written = loop.get("gate_steps")
    unkeyed = set(loop.get("select", {}).get("unkeyed", []))
    if not written:
        command, words, sets = gate(passed)
        return [
            {
                "name": WHOLE,
                "command": command,
                "words": words,
                "sets": sets,
                "where": "any",
                "legacy": True,
                "paths": None,
                "setting": None,
                "unkeyed": bool({command, loop.get("gate")} & unkeyed),
                "on_demand": False,
            }
        ]
    if loop.get("gate"):
        raise Refused(
            f"{source(project()[1])} sets both `gate` and `gate_steps`: keep `gate_steps`, the gate's steps, and"
            " drop `gate`"
        )
    plan = []
    for step in written:
        command, words, sets = command_line(step["command"], f"gate_steps.{step['name']}", passed)
        plan.append(
            {
                "name": step["name"],
                "command": command,
                "words": words,
                "sets": sets,
                "where": step["where"],
                "legacy": False,
                "paths": step.get("paths"),
                "setting": written,
                "unkeyed": bool({step["name"], step["command"], command} & unkeyed),
                "on_demand": bool(step.get("on_demand")),
            }
        )
    if all(step["on_demand"] for step in plan):
        raise Refused(
            f"{source(project()[1])}: every `gate_steps` step sets `on_demand: true`, so the gate would have no step"
            " and pass every head with no run: list the merge check as a step without `on_demand`"
        )
    if asked := ASKED.get():
        return [step for step in plan if step["name"] in asked]
    return [step for step in plan if every or not step["on_demand"]]


def skipped(step: dict[str, Any]) -> str:
    """The line a gate run says for an `on_demand` step it does not run (#2295)."""
    return (
        f"{step['name']}: skipped, on demand (`on_demand: true`): it runs only when named,"
        f" `local-ci --record --step {step['name']}`, and the gate's pass does not need it"
    )


def inputs_key(tree: str, step: dict[str, Any]) -> str | None:
    """A gate step's inputs key on `tree` (#963): a digest of the `gate_steps` setting as read (which holds every
    step's command and `paths`) and the mode, object id and path of each file under the step's `paths` in `tree` (globs
    matched on the full path, case and all, so every host reads the same files), or None, which keys the step on the
    whole tree as #940 did: a step without `paths`, an empty one, one matching no file in `tree`, or a tree git can't
    list. A pass is found by its command (the record's name, recorded) and this key, so any tree with the same key
    reuses it: a change outside the step's paths runs nothing again, while a change to the setting, the command a
    head proof runs (gate) or a file the step reads gives another."""
    globs = step.get("paths")
    if not globs:
        return None
    try:
        listed = git("ls-tree", "-r", "-z", "--full-tree", tree)
    except (subprocess.CalledProcessError, OSError):
        return None
    read = [
        entry
        for entry in listed.split("\0")
        if "\t" in entry
        and any(fnmatch.fnmatchcase(entry.split("\t", 1)[1], glob) for glob in globs)
    ]
    if not read:
        return None
    digest = hashlib.sha256()
    for part in (json.dumps(step["setting"], sort_keys=True), *sorted(read)):
        digest.update(part.encode() + b"\0")
    return digest.hexdigest()


def here(step: dict[str, Any]) -> bool:
    """Whether `step` may run here: a `host` step never runs in a cloud session (in_cloud), a `linux` step only
    where `sys.platform` is Linux (a Linux host or a cloud session), never on macOS."""
    if step["where"] == "linux":
        return sys.platform.startswith("linux")
    return step["where"] != "host" or not in_cloud()


def runs_only(left: list[str]) -> str:
    """`left` steps by the place their `where` limits them to: `a, b runs only on a host (`where: host`)`, one clause per `where`."""
    wheres = {step["name"]: step["where"] for step in gate_plan()}
    places = {"host": "a host", "linux": "Linux"}
    clauses = []
    for where in dict.fromkeys(wheres.get(name, "host") for name in left):
        names = ", ".join(f"`{name}`" for name in left if wheres.get(name, "host") == where)
        clauses.append(f"{names} runs only on {places.get(where, where)} (`where: {where}`)")
    return "; ".join(clauses)


def command_line(
    written: str, key: str, passed: list[str] | tuple[str, ...] = ()
) -> tuple[str, list[str], dict[str, str]]:
    """loop.md's command `key` (`gate`, `pre_land`) as written: its words, and the variables it sets in front of them,
    each of `passed` this process's environment sets taking that value (gate). It runs with no shell, so a shell
    operator (`&&`, `;`, `|`) is refused: the words after it would be arguments, never a command."""
    lexer = shlex.shlex(written, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    words = list(lexer)
    assigned = len(
        list(
            itertools.takewhile(
                lambda word: re.fullmatch(r"[A-Za-z_]\w*=.*", word, re.DOTALL), words
            )
        )
    )
    if (
        assigned == len(words)
        or any(set(word) <= set("();<>|&") for word in words)
        or re.fullmatch(r"\w*=.*", words[assigned], re.DOTALL)
    ):
        raise Refused(
            f"loop.md's `{key}` is {written!r}: variables to set, then one command to run, with no shell operator"
            " (put a chain of checks in one task)"
        )
    sets = dict(word.split("=", 1) for word in words[:assigned])
    if caller := {name: os.environ[name] for name in passed if name in os.environ}:
        sets |= caller
        words = [*(f"{name}={value}" for name, value in sets.items()), *words[assigned:]]
        assigned = len(sets)
    return shlex.join(words), words[assigned:], sets


def ran_gate(
    cwd: Path | None = None, out: Any = None, passed: list[str] | tuple[str, ...] = ()
) -> int:
    """Run each step of the gate that may run here (gate_plan, here), in order, to the first that fails, and return
    that step's exit, else 0; nothing is recorded (a checkout with changes)."""
    for step in gate_plan(passed, every=True):
        if step["on_demand"] and not ASKED.get():
            print(skipped(step))
        elif here(step) and (code := ran_step(step, cwd, out)[0]):
            return code
    return 0


def heavy(words: list[str]) -> int:
    """`heavy <command> [args...]`: run one command as a heavy step (run_heavy), for the role files' shell recipes, and
    exit with its exit code (128 plus the signal that ended it); 127 for a command that can't start, as a shell says.
    `heavy --plan` prints the host's slot count and the limit that set it (slot_plan's line) and exits 0."""
    if words == ["--plan"]:
        print(slot_plan()[1])
        return 0
    if words[:1] == ["--"]:
        words = words[1:]
    if not words:
        raise Refused("heavy takes the command to run: `approvals.py heavy <command> [args...]`")
    try:
        code = run_heavy(words)[0]
    except OSError as exc:
        print(f"heavy: {words[0]} can't run: {exc}", file=sys.stderr)
        return 127
    return 128 - code if code < 0 else code


def pr_base_of_head() -> str | None:
    """The base of the open PR whose branch is the checkout's (open_pulls), else None: a detached HEAD, no PR for it
    yet, or a forge that doesn't answer is no PR, and the caller falls back to the default branch."""
    with contextlib.suppress(subprocess.CalledProcessError, OSError, Refused):
        branch = git("branch", "--show-current").strip()
        for pull in open_pulls() if branch else []:
            if pull.get("headRefName") == branch and pull.get("baseRefName"):
                return str(pull["baseRefName"])
    return None


def gate_base_variable(text: str | None = None) -> str:
    """The variable loop.md's line `Gate base:` names (of `text`, else the policy's loop.md), else `GATE_BASE`; refused
    when it is no variable name."""
    text = (policy_file(toplevel(), LOOP, POLICY_BASE.get()) or "") if text is None else text
    named = re.search(r"^Gate base:[ \t]*`?([^\s`]+)", text, re.MULTILINE)
    variable = named.group(1) if named else "GATE_BASE"
    if not re.fullmatch(r"[A-Za-z_]\w*", variable):
        raise Refused(f"loop.md's `Gate base:` names {variable!r}, which is no variable name")
    return variable


# loop.md's narrowed mode (SKILL.md, Adopting the loop): its `Affected tests:` command is the whole narrowed check, and
# the line `Gate: full` opts the project out, so every gate runs the full check as it does without the selector
AFFECTED = re.compile(r"^Affected tests:[ \t]*(.+?)[ \t]*$", re.MULTILINE)
# where the `Affected tests:` command writes its JUnit XML report, which rerun-tests reads to count the tests it ran
TEST_REPORT = re.compile(r"^Test report:[ \t]*`?(.+?)`?[ \t]*$", re.MULTILINE)
FULL_GATE = re.compile(r"^Gate:[ \t]*`?full`?[ \t]*$", re.MULTILINE)
# the end of a narrowed gate's command (narrowed_gate): `(` is a shell operator command_line refuses, and the command it
# returns is shlex-quoted, so no full gate's command, record or key can ever equal a narrowed one
NARROWED = " (narrowed)"
# the base a narrowed gate selects against, set by narrowed_to; None runs the full gate
NARROW_BASE: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "NARROW_BASE", default=None
)
# the head a narrowed gate is of, set by narrowed_to: a PR's head read from any checkout; None is the checkout itself
NARROW_HEAD: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "NARROW_HEAD", default=None
)
# set by `local-ci --record --rerun` (local_ci): no step's pass is reused (step_pass), here or on origin, since a rerun
# that reused a record would prove only what that record already said
FRESH: contextvars.ContextVar[bool] = contextvars.ContextVar("FRESH", default=False)
# set by `local-ci --record --rerun --no-push`: the rerun's record commit is made and kept as a local branch, not
# pushed (publish), for a CI job whose token can't write: another job, holding no code of the PR, pushes it
KEEP: contextvars.ContextVar[bool] = contextvars.ContextVar("KEEP", default=False)
# set by `local-ci --record --step <name>`: the gate is those named steps alone (gate_plan), an `on_demand` one included,
# and its run is no run of the whole gate, so it writes no `gate` status or event (gate_branch, local_ci)
ASKED: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar("ASKED", default=())
# the line that names a kept record, `<ref> <commit>`, which that job reads
KEPT = "rerun record kept, not pushed: "


def loop_md() -> str:
    """loop.md's text as project() reads it: its policy when that is loop.md's (no gatekeeper.md), else the file
    (policy_file); empty without one. A policy project() refuses is no reason to refuse a line outside it."""
    with contextlib.suppress(Refused):
        loop = project()[1]
        if loop is not None and not isinstance(loop, Policy):
            return loop
    return policy_file(toplevel(), LOOP, POLICY_BASE.get()) or ""


def narrowing(text: str | None = None) -> str | None:
    """loop.md's `Affected tests:` command (of `text`, else loop_md), or None: no line, or the opt-out `Gate: full`."""
    text = loop_md() if text is None else text
    found = AFFECTED.search(text)
    if found is None or FULL_GATE.search(text):
        return None
    return found.group(1).strip().strip("`")


@contextlib.contextmanager
def narrowed_to(base: str | None, head: str | None = None) -> Any:
    """Within the block, gate() and gate_plan() are the narrowed gate against `base` (narrowed_gate); None: full.
    `head` is the commit the gate is of, when it is not the checkout (a PR's head, from a checkout anywhere)."""
    token, head_token = NARROW_BASE.set(base), NARROW_HEAD.set(head)
    try:
        yield
    finally:
        NARROW_HEAD.reset(head_token)
        NARROW_BASE.reset(token)


def narrowed_gate(
    passed: list[str] | tuple[str, ...] = (),
) -> tuple[str, list[str], dict[str, str]] | None:
    """Under narrowed_to a base, in narrowed mode (narrowing) and with no `gate_steps` (whose steps narrow by their
    `paths`): the `Affected tests:` command (with `passed`, as gate() reads them), with the `Gate base:` variable
    (gate_base_variable) set to the base, as gate() returns a gate; else None. Its command is that variable's
    assignment first, then the rest, then NARROWED, so a record of it is keyed by the command and the base and never
    stands for a full pass. A change gatekeeper.md's `select` finds no build input in (selected) runs `affected-tests
    --base <base>` instead, which runs only the checks it touches, and its command names those checks (`select.checks:
    <command>...`, or `none`), so a docs or pack-bump head's record is keyed by what ran."""
    base = NARROW_BASE.get()
    if base is None or settings(project()[1]).get("gate_steps"):
        return None
    if (written := narrowing()) is None:
        return None
    variable = gate_base_variable()
    if (cut := selected(base, NARROW_HEAD.get())) is not None and not cut[0]:
        # no build input changed (select): the run is affected-tests' own cut, keyed by the checks it runs
        said = shlex.join(["select.checks:", *cut[1]] if cut[1] else ["select.checks:", "none"])
        words = [
            sys.executable,
            str(Path(__file__).with_name("gatekeeper.py")),
            "affected-tests",
            "--base",
            base,
        ]
        # the env is the command's own assignment, as gate_record reads it back from the key (command_line)
        return f"{variable}={base} {said}{NARROWED}", words, {variable: base}
    _, words, sets = command_line(written, "Affected tests", passed)
    sets = {variable: base, **{name: value for name, value in sets.items() if name != variable}}
    command = shlex.join([*(f"{name}={value}" for name, value in sets.items()), *words])
    return command + NARROWED, words, sets


def narrowed_base(command: str) -> str | None:
    """The base a narrowed gate's command (narrowed_gate) selected against, None for a full gate's command."""
    if not command.endswith(NARROWED):
        return None
    return shlex.split(command[: -len(NARROWED)])[0].split("=", 1)[1]


def selection_base(given: str | None) -> str | None:
    """The commit a narrowed run selects against: `given` (`--base`) resolved, else `git merge-base origin/<base>
    HEAD`, `<base>` being the base of the open PR for the checked-out branch, else the default branch, a shallow clone
    deepened first (deepened). None, with one line saying so, for a `given` git can't resolve (all zeros, the push of
    a new branch)."""
    if given is not None:
        try:
            return git("rev-parse", "--verify", "--quiet", f"{given}^{{commit}}").strip()
        except subprocess.CalledProcessError:
            print(f"the base {given} is no commit here", file=sys.stderr)
            return None
    base = pr_base_of_head() or base_branch(None)
    if base is None:
        raise Refused("no default branch to select the affected tests against: fetch origin")
    with policy_from(base):
        deepened(None)
    try:
        return git("merge-base", f"origin/{base}", "HEAD").strip()
    except subprocess.CalledProcessError as exc:
        raise Refused(
            f"no merge base of origin/{base} and HEAD: fetch origin/{base} deeper"
        ) from exc


# what is never a build input, whatever a project declares (select.build_inputs): docs, prose, an agent's config and
# vendored skills, the pack's own pin. fnmatch's `*` crosses `/`, so `docs/*` is the whole tree under docs.
NON_PRODUCT = ("docs/*", "*.md", ".agents/*", ".claude/*", "apm.yml", "apm.lock.yaml")
# the policy the selection reads: a change to it is a build input, since the base's rule can't judge its own edit
SELECTION_FILES = (str(POLICY), str(LOOP))


def classify(files: list[str], select: dict[str, Any]) -> tuple[list[str], list[str]]:
    """The narrowed gate's first cut, by gatekeeper.md's `select`: the changed `files` that are build inputs, and the
    `checks` commands the rest touch. A file is a build input when it matches `build_inputs`, is the selection's own
    policy (SELECTION_FILES), or matches neither NON_PRODUCT nor a check's `paths`: a path whose effect on the build
    is unclear tests. Without `select` every file is one, as before the setting."""
    if "build_inputs" not in (
        select or {}
    ):  # no declaration: every file tests, as before the setting
        return list(files), []
    checks = select.get("checks", [])
    inputs, commands = [], []
    for path in files:
        hit = [
            check["command"]
            for check in checks
            if any(fnmatch.fnmatch(path, g) for g in check["paths"])
        ]
        if (
            path in SELECTION_FILES
            or any(fnmatch.fnmatch(path, glob) for glob in select["build_inputs"])
            or not (hit or any(fnmatch.fnmatch(path, glob) for glob in NON_PRODUCT))
        ):
            inputs.append(path)
        commands += [command for command in hit if command not in commands]
    return inputs, commands


def changed_since(base: str, head: str | None = None) -> list[str] | None:
    """The files `head` (else the checkout's HEAD) changes since `base` (a moved file both names), None when git
    can't tell: a diff git can't list, or, for the checkout, uncommitted or untracked changes, which no commit holds."""
    if head is None and checkout_state()[1]:
        return None
    try:
        found = git("diff", "--name-only", "--no-renames", "-z", f"{base}...{head or 'HEAD'}")
    except subprocess.CalledProcessError:
        return None
    return found.split("\0")[:-1]


def selected(base: str, head: str | None = None) -> tuple[list[str], list[str]] | None:
    """classify() of the change from `base` to `head` (else the checkout, changed_since) by gatekeeper.md's `select`:
    (its build inputs, the checks the rest touch); None without `select.build_inputs`, under a policy that can't be
    read, or when git can't list the change, since the `Affected tests:` command then tests everything it would have."""
    select = None
    with contextlib.suppress(Refused):
        select = settings(project()[1]).get("select")
    if not select or "build_inputs" not in select:
        return None
    files = changed_since(base, head)
    return None if files is None else classify(files, select)


def non_product_run(base: str, root: Path, env: dict[str, str]) -> int | None:
    """affected-tests' cut by `select` (selected): None when the change since `base` holds a build input, or the cut
    can't tell, so the caller's run goes ahead (the `Affected tests:` command, else the full check); else the exit of the `checks` the change touches, each run in
    turn under the gate slot, the first that fails ending the run, 0 when none applies."""
    if (cut := selected(base)) is None:
        return None
    inputs, commands = cut
    if inputs:
        print(
            f"affected-tests: build input {inputs[0]}: the cut can't settle it",
            file=sys.stderr,
        )
        return None
    if not commands:
        print("affected-tests: 0 selected: non-product", file=sys.stderr)
        return 0
    for command in commands:
        _, words, sets = command_line(command, "select.checks")
        print(
            f"affected-tests: no build input changed: {shlex.join(words)}",
            file=sys.stderr,
            flush=True,
        )
        try:
            with heavy_slot("affected-tests") as (slot, _):
                code = run_heavy(words, env=env | sets | {SLOT_HELD: slot}, cwd=root)[0]
        except OSError as exc:
            raise Refused(f"`{words[0]}` can't run: {exc}") from None
        if code:
            return code
    return 0


def affected_tests(given: str | None = None, record: bool = False, text: str | None = None) -> int:
    """`affected-tests [--base <rev>]`: the worker's test run on a head, from loop.md's line `Affected tests:
    <command>` (the command selects and runs the tests a change affects against the base it reads from the variable
    the line `Gate base:` names, else `GATE_BASE`). The base is `<rev>`, else the merge base (selection_base). Where it
    can't tell exactly, it runs the narrowest set it can still name (#2217): a `<rev>` git can't resolve (all zeros, a
    new branch) selects against the merge base instead, and a loop.md without the line runs gatekeeper.md's `select`
    cut (non_product_run), then, for a change that cut can't settle, the project's full check, `mise run check`. Zero
    runs only for a change `select` finds only non-product paths in, saying `0 selected: non-product`. The opt-out
    `Gate: full` (narrowing), the project's own choice, runs `mise run check` too, with the variable unset. Either
    runs under the host gate slot (heavy_slot, run_heavy), as a `local-ci` step does. Its exit passes through: a
    selector that selects nothing exits 0 itself. A run still going at the policy's `timeouts.run` (60 minutes by
    default) is stopped, with its process group, and exits TIMED_OUT (124) with one line naming the budget and the
    selection's size (over_budget): a timeout, never a test's failure. gate_env clears the variable from `land`'s and
    `local-ci`'s environment, apart from a head proof's own `env` names: they set it themselves in narrowed mode
    (narrowed_gate).

    With `record`, a narrowed run is the narrowed gate against that base, run and recorded by local_ci as `local-ci
    --record` runs one (run_local_ci: a clean checkout, a pass recorded by tree and command, pushed as its
    `loop-gate/<tree>-<key>` branch, publish), so its record is the one record kind, read by `gate-record <head> --
    <command>` and by the merge queue's floor (engineering-loop SKILL.md, R6), and a pass already recorded on the tree
    is reused, not run again (R1). A run with no `Affected tests:` line, one that fails, the opt-out's full check and a
    gate with `gate_steps` (whose steps narrow by their `paths`, so the narrowed gate never runs) record nothing: the
    last says so. Off by default, as `local-ci` records only with `--record`: a record is a pushed branch, and only
    the worker's final head needs one, not each head the push hook runs.

    `text`, when given, is the loop.md the command and its `Gate base:` come from instead of the policy's: a rerun's
    (rerun_tests), the red head's own."""
    root = toplevel()
    text = loop_md() if text is None else text
    written = narrowing(text)
    variable = gate_base_variable(text)
    env = {name: value for name, value in os.environ.items() if name != variable}
    words, sets = [*LOCAL_CI[:-1], "check"], {}
    if not FULL_GATE.search(text):
        if (base := selection_base(given)) is None:
            print(
                f"affected-tests: the base {given} is no commit here: it selects against the merge base",
                file=sys.stderr,
            )
            base = selection_base(None)
        if record and written is not None:
            if settings(project()[1]).get("gate_steps"):
                print(
                    "affected-tests --record: the gate has `gate_steps`, whose steps narrow by their `paths`, so the"
                    " narrowed gate never runs: nothing recorded, and `local-ci --record` records the steps",
                    file=sys.stderr,
                )
            else:
                with narrowed_to(base):
                    return local_ci(None, None, False, None)
        if (code := non_product_run(base, root, env)) is not None:
            return code
        if written is None:
            print(
                "affected-tests: loop.md names no `Affected tests:` selector, so it can't narrow the change: the"
                " project's full check runs",
                file=sys.stderr,
            )
        else:
            _, words, sets = command_line(written, "Affected tests")
            sets = {
                name: value for name, value in sets.items() if name != variable
            }  # the base is the run's, not the line's
            env[variable] = base
    print(f"affected-tests: {shlex.join(words)}", file=sys.stderr, flush=True)
    budget = run_budget()
    # ponytail: the pack sees the selection only where it makes it (the whole suite): a project's `Affected tests:`
    # command picks its tests internally, so no list-only mode yet (#2445's open question) and no estimate for it
    estimate: float | None = None
    if written is None or FULL_GATE.search(text):
        try:
            shards = shard_count()
        except Refused:
            shards = 1
        estimate, why = read_estimate(root, shards)
        cause = (
            "`Gate: full` selects the whole suite"
            if FULL_GATE.search(text)
            else "loop.md names no `Affected tests:` selector, so the whole suite"
        )
    else:
        why = "the project's `Affected tests:` command picks its tests itself, so the pack has no list to estimate"
    if estimate is None:
        print(
            f"affected-tests: no estimate available ({why}): `timeouts.run` ({said_span(budget)}) is the only limit",
            file=sys.stderr,
        )
    elif estimate > FAIL_FAST_SHARE * budget:
        print(
            f"affected-tests: the estimate {said_span(int(estimate))} is over {FAIL_FAST_SHARE:.0%} of `timeouts.run`"
            f" ({said_span(budget)}): {cause}; raise `timeouts.run`, shard it or narrow the run",
            file=sys.stderr,
        )
        return TIMED_OUT
    else:
        print(
            f"affected-tests: estimate {said_span(int(estimate))} of `timeouts.run` ({said_span(budget)})",
            file=sys.stderr,
        )
    started = time.monotonic()
    try:
        with heavy_slot("affected-tests") as (slot, _):
            started = time.monotonic()
            code, spent = run_heavy(
                words, env=env | sets | {SLOT_HELD: slot}, budget=budget, cwd=root
            )
    except OSError as exc:
        raise Refused(f"`{words[0]}` can't run: {exc}") from None
    if estimate is not None:  # no estimate, nothing to compare, and no file to dirty a checkout
        note_run(root, estimate, time.monotonic() - started, code)
    if spent is SPENT:
        print(over_budget(budget, root, base if written is not None else None), file=sys.stderr)
    return code


# the per-test durations the nightly records (`durations --record`), the file `shards --durations` reads, and the
# estimate beside the actual time of each gate run (#2445); under the working tree's `.qualify/`, scratch the repos ignore
DURATIONS_FILE = Path(".qualify/durations.json")
GATE_RUNS_FILE = Path(".qualify/gate-runs.jsonl")
SETUP_FILE = Path(".qualify/setup-seconds")
FAIL_FAST_SHARE = (
    0.8  # the share of `timeouts.run` an estimate may take: runner speed varies from run to run
)
DRIFT_SHARE = 0.25  # how far the actual time may be from its estimate before the nightly report flags it (a start)
DURATION_LINE = re.compile(r"^\s*([\d.]+)s\s+(?:setup|call|teardown)\s+(\S+)", re.MULTILINE)


def durations_path(root: Path) -> Path:
    """The durations file of the project at `root`: `GATE_DURATIONS`, else `.qualify/durations.json`."""
    return Path(os.environ.get("GATE_DURATIONS") or root / DURATIONS_FILE)


def parse_durations(text: str) -> dict[str, float]:
    """Seconds per test file from a JUnit XML (`testcase` times, by its `file`, else its classname's module) or from
    `pytest --durations=0` output (setup, call and teardown lines, by the node id's file): the JSON `shards
    --durations` reads."""
    found: dict[str, float] = {}
    if text.lstrip().startswith("<"):
        for case in ET.fromstring(text).iter("testcase"):
            file = case.get("file")
            if not file:
                parts = (case.get("classname") or "").split(".")
                while parts and parts[-1][:1].isupper():
                    parts.pop()  # a test class, not a module
                file = "/".join(parts) + ".py" if parts else ""
            if file:
                found[file] = found.get(file, 0.0) + float(case.get("time") or 0)
    else:
        for seconds, node in DURATION_LINE.findall(text):
            file = node.split("::")[0]
            if not file:  # a nested pytest run's `::test` line: no file of this run
                continue
            found[file] = found.get(file, 0.0) + float(seconds)
    return {file: round(seconds, 3) for file, seconds in found.items()}


def estimate_seconds(
    items: Sequence[str],
    durations: dict[str, float],
    workers: int = 1,
    setup: float = 0.0,
    shards: int = 1,
) -> float | None:
    """The expected run time of `items`: their recorded seconds (an unrecorded one at the mean of the recorded) over
    `workers`, the longest shard's when `shards` splits them (shard_selections), plus the measured fixed `setup`
    (install, collection). None with no recorded duration to go on."""
    chosen = list(dict.fromkeys(items))
    known = {item: durations[item] for item in chosen if item in durations}
    if not known:
        return None
    mean = sum(known.values()) / len(known)
    held = shard_selections(chosen, chosen, shards, known, whole=True) if shards > 1 else [chosen]
    longest = max(sum(known.get(item, mean) for item in group) for group in held)
    return longest / max(workers, 1) + setup


def read_estimate(root: Path, shards: int) -> tuple[float | None, str]:
    """The whole suite's estimate from the recorded durations, else None with why there is none."""
    path = durations_path(root)
    try:
        durations = {k: float(v) for k, v in json.loads(path.read_text()).items()}
    except (OSError, ValueError, AttributeError, TypeError):
        return None, f"no recorded durations at {path.name}: the nightly has not recorded any"
    try:
        workers = int(os.environ.get("GATE_WORKERS", "1"))
    except ValueError:
        workers = 1
    try:
        setup = float(os.environ.get("GATE_SETUP_SECONDS") or (root / SETUP_FILE).read_text())
    except (OSError, ValueError):
        setup = 0.0
    # a test file deleted since the nightly recorded it is no part of the run: a stale file must not over-estimate
    durations = {
        item: secs for item, secs in durations.items() if (root / item.split("::")[0]).exists()
    }
    found = estimate_seconds(list(durations), durations, workers, setup, shards)
    return found, "no recorded durations" if found is None else ""


def note_run(root: Path, estimate: float | None, actual: float, code: int) -> None:
    """Append the gate run's estimate beside its actual time to `.qualify/gate-runs.jsonl`, for the nightly report."""
    path = root / GATE_RUNS_FILE
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as out:
            line = {
                "at": int(time.time()),
                "estimate": estimate,
                "actual": round(actual, 2),
                "exit": code,
            }
            out.write(json.dumps(line) + "\n")


def drift_lines(root: Path, share: float = DRIFT_SHARE) -> list[str]:
    """One line for each recorded gate run whose actual time is over `share` away from its estimate."""
    try:
        rows = [
            json.loads(line) for line in (root / GATE_RUNS_FILE).read_text().splitlines() if line
        ]
    except (OSError, ValueError):
        return []
    return [
        f"drift: a gate run at {row['at']} took {said_span(int(row['actual']))} against an estimate of"
        f" {said_span(int(row['estimate']))} ({(row['actual'] - row['estimate']) / row['estimate']:+.0%}): the durations"
        " file is stale or the setup figure is wrong"
        for row in rows
        if row.get("estimate") and abs(row["actual"] - row["estimate"]) / row["estimate"] > share
    ]


def durations_verb(record: str | None, out: str | None, setup: float | None, report: bool) -> int:
    """`durations --record <junit.xml | durations.txt> [--out <file>] [--setup <seconds>]` turns one run's JUnit XML or
    `--durations=0` output into the durations file `shards --durations` and `affected-tests` read; `--report` prints a
    line for each recorded gate run whose time is far from its estimate (the nightly report)."""
    root = toplevel()
    if record:
        found = parse_durations(Path(record).read_text())
        if not found:
            print(f"durations: no test times in {record}", file=sys.stderr)
            return 1
        target = Path(out) if out else durations_path(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(found, indent=1, sort_keys=True) + "\n")
        if setup is not None:
            (root / SETUP_FILE).parent.mkdir(parents=True, exist_ok=True)
            (root / SETUP_FILE).write_text(f"{setup}\n")
        print(f"durations: {len(found)} test files, {sum(found.values()):.0f} seconds, in {target}")
    if report or not record:
        lines = drift_lines(root)
        print(
            "\n".join(lines)
            if lines
            else "durations: every recorded estimate is near its run's time"
        )
    return 0


def run_budget() -> int:
    """`timeouts.run` in seconds for a selection run: the default where the policy can't be read, with one line saying
    so, since `affected-tests` ran before it read any policy and a project without the block must keep running it."""
    try:
        return timeouts(project()[1])["run"]
    except Refused as refusal:
        fallback = timeouts(None)["run"]
        print(
            f"affected-tests: the policy can't be read ({refusal}), so the run's budget is the default"
            f" {said_span(fallback)}",
            file=sys.stderr,
        )
        return fallback


def over_budget(budget: int, root: Path, base: str | None) -> str:
    """The one line of a selection run stopped at `timeouts.run`: the budget, the selection's size as far as the verb
    knows it (the whole suite under `Gate: full`; else unknown, since the project's own command selects, with the
    count of files the change touches against `base`, the selector's input), and that it ran unsharded: this verb runs
    the project's one command, so with `select.shards` above 1 the line names the `shards` verb that splits a selection
    (#2229)."""
    if base is None:
        size = "the whole suite (`Gate: full`)"
    else:
        try:
            count = str(len(git("-C", str(root), "diff", "--name-only", base, "HEAD").split()))
        except subprocess.CalledProcessError:
            count = "an unknown number of"
        size = (
            f"a selection of unknown size ({count} changed file{'' if count == '1' else 's'} against"
            f" {base[:12]})"
        )
    try:
        shards = shard_count()
    except Refused:
        shards = 1
    unsharded = (
        "unsharded (no `select.shards`)"
        if shards == 1
        else f"unsharded (`select.shards` is {shards}: split the selection with `approvals.py shards`)"
    )
    return (
        f"affected-tests: timed out after `timeouts.run` ({said_span(budget)}) running {size}, {unsharded}: a"
        f" timeout, not a test failure; raise `timeouts.run` in gatekeeper.md or narrow the run"
    )


def shard_count() -> int:
    """`select.shards` from gatekeeper.md: how many shards a whole-suite selection splits into; 1 when absent. A value
    below 1 is refused, since there is no run with no shard."""
    count = settings(project()[1]).get("select", {}).get("shards", 1)
    if count < 1:  # the schema has refused a value that is no whole number
        raise Refused(f"gatekeeper.md's select.shards must be at least 1, not {count}")
    return count


def shard_selections(
    selection: list[str],
    suite: list[str],
    count: int,
    durations: dict[str, float] | None = None,
    whole: bool = False,
    limit: float | None = None,
) -> list[list[str]]:
    """`selection` (items of `suite`, one test target each) as `count` disjoint shards whose union is the selection,
    balanced by `durations` (seconds per item, from the last full run): each item, longest first, goes to the
    shard with the least time so far; an item with no recorded duration counts as the mean of the recorded ones (all
    equal, so an even split by count, with none recorded), so it still lands in exactly one shard. Only a whole-suite
    selection splits: `whole` (a build input changed, or the nightly), one over half of `suite`, or one whose estimate (the
    sum of its items' seconds, an unrecorded item at the mean; 0 with no durations) is over `limit` (`timeouts.run`,
    in seconds). A narrow selection (none of those), `count` 1 or less, or an empty one is returned as the one selection, unchanged. `count` is capped at the number of
    items, so no shard is empty. Each shard keeps the selection's order."""
    chosen = list(dict.fromkeys(selection))
    if count <= 1 or not chosen:
        return [selection]
    known = {item: durations[item] for item in chosen if durations and item in durations}
    fallback = sum(known.values()) / len(known) if known else 1.0
    estimate = sum(known.get(item, fallback) for item in chosen) if known else 0.0
    over = limit is not None and estimate > limit
    if not (whole or over or len(chosen) * 2 > len(set(suite))):
        return [selection]
    count = min(count, len(chosen))
    load, shards = [0.0] * count, [[] for _ in range(count)]
    order = sorted(range(len(chosen)), key=lambda i: (-known.get(chosen[i], fallback), i))
    for at in order:
        lightest = min(range(count), key=lambda shard: (load[shard], shard))
        shards[lightest].append(at)
        load[lightest] += known.get(chosen[at], fallback)
    return [[chosen[at] for at in sorted(held)] for held in shards]


def shard_command(command: str, shard: int, count: int, items: list[str]) -> str:
    """The command a shard's pass is recorded under: `command` with `GATE_SHARD=<shard>/<count>:<digest of the
    shard's items>` set in front, so R1's record (record_name: tree and command) holds one pass per shard, and a
    shard whose selection changed is another record. A single shard is keyed the same way, by its items: a run of some
    of the tests must never stand on record for the command's full run."""
    return f"GATE_SHARD={shard_key(shard, count, items)} {command}"


def shard_key(shard: int, count: int, items: list[str]) -> str:
    """`<shard>/<count>:<digest of the shard's items>`: the value of `GATE_SHARD` that a shard's record names (its
    env) and its command sets, so a split that changed (new durations, a changed suite) is another record."""
    return f"{shard}/{count}:{hashlib.sha256(chr(10).join(items).encode()).hexdigest()[:12]}"


def sharded(args: argparse.Namespace) -> int:
    """`shards --suite <file> [--selection <file>] [--whole] [--durations <file>]` prints the selection (default: the
    whole suite) split by `select.shards` (shard_count, shard_selections). With one shard it prints the selection as it
    is; with more it prints, for each shard, a `# shard i/n <key>` line and its items. `--durations` is a JSON file of
    seconds per item; one that is missing or unreadable is said on stderr, and the split is even by count. A count
    above the number of items is capped at it, said on stderr.

    `--command <cmd>` names the check the shards run. With `--passed <head>` it prints, for each shard (gate_record),
    the record branch found or the line `no record of ...: run it`, and exits 0 when every shard has a pass of the
    command on <head>'s tree (origin's record), else 1. With `--record <head> --shard <i>` it runs shard <i>: `<cmd>`
    with the shard's items as its last arguments and `GATE_SHARD` set to the shard's key (shard_key), under the host
    gate slot, in this checkout, which must be at <head>; on exit 0 it pushes R1's record of that run (push_record,
    keyed by the tree and the shard's command, the env `GATE_SHARD`) and exits 0, else it records nothing and exits
    with the command's exit."""
    suite = Path(args.suite).read_text().split()
    selection = Path(args.selection).read_text().split() if args.selection else suite
    durations: dict[str, float] | None = None
    if args.durations:
        try:
            durations = {
                k: float(v) for k, v in json.loads(Path(args.durations).read_text()).items()
            }
        except (OSError, ValueError, AttributeError, TypeError) as exc:
            print(
                f"shards: no durations from {args.durations} ({exc}): even split by count",
                file=sys.stderr,
            )
    wanted = shard_count()
    shards = shard_selections(
        selection,
        suite,
        wanted,
        durations,
        args.whole or not args.selection,
        timeouts(project()[1])["run"],
    )
    if 1 < len(shards) < wanted:
        print(
            f"shards: {wanted} shards asked, but {len(shards)} items: {len(shards)} shards",
            file=sys.stderr,
        )
    if (args.passed or args.record) and not args.gate_command:
        raise Refused("shards --passed and --record take the --command whose shards they read")
    if args.passed:
        missing = sum(
            gate_record(
                args.passed, shlex.split(shard_command(args.gate_command, at, len(shards), items))
            )
            != 0
            for at, items in enumerate(shards, 1)
        )
        return 1 if missing else 0
    if args.record:
        return record_shard(args.record, args.shard, shards, args.gate_command)
    if len(shards) == 1:
        print("\n".join(shards[0]))
        return 0
    for at, items in enumerate(shards, 1):
        print(f"# shard {at}/{len(shards)} {shard_command('', at, len(shards), items).strip()}")
        print("\n".join(items))
    return 0


def record_shard(head: str, shard: int | None, shards: list[list[str]], command: str) -> int:
    """`shards --record <head> --shard <i>`: run shard <i> of `shards` and, when it passes, push R1's record of it
    (see sharded), under the guards `local-ci --record` has (run_local_ci): the checkout must be at `head` and clean
    before the run (checkout_state), and a run that changes it, or moves HEAD (head_moved), records nothing. `command`
    is read as loop.md's gate is (command_line: variables to set, then one command), and runs in gate_env's
    environment, with the variables it sets and `GATE_SHARD`, so the record's env is what `--passed` keys it by."""
    if shard is None or not 1 <= shard <= len(shards):
        raise Refused(f"shards --record takes --shard 1 to {len(shards)}")
    before = checkout_state()
    try:
        at_head = before[0] == git("rev-parse", "--verify", f"{head}^{{commit}}").strip()
    except subprocess.CalledProcessError:  # a head this clone has no commit for
        at_head = False
    if not at_head:
        raise Refused(f"shards --record runs on {head}: this checkout is at another commit")
    if before[1]:
        raise Refused(f"the tree has changes, so the check would not run on the head:\n{before[1]}")
    tree = git("rev-parse", "HEAD^{tree}").strip()
    items = shards[shard - 1]
    keyed = shard_command(command, shard, len(shards), items)
    recorded, words, env = command_line(
        keyed, "shards --command"
    )  # `recorded`: the form `gate()` records
    words = [*words, *items]
    try:
        with heavy_slot("shards") as (slot, waited):
            began = time.monotonic()
            code = run_heavy(
                words, env=gate_env({"sets": env}) | {SLOT_HELD: slot}, cwd=toplevel()
            )[0]
    except OSError as exc:
        raise Refused(f"`{words[0]}` can't run: {exc}") from None
    if code != 0:
        print(f"shard {shard}/{len(shards)} exited {code}: nothing recorded")
        return code
    now_state = checkout_state()
    if moved := head_moved(
        before[0], tree, now_state[0], git("rev-parse", "HEAD^{tree}").strip()
    ) or ("" if now_state == before else now_state[1]):
        raise Refused(
            f"the shard changed the checkout, so it did not run on the head as pushed:\n{moved}"
        )
    ref = push_record(
        {
            "head": before[0],
            "tree": tree,
            "command": recorded,
            "exit": 0,
            "env": env,
            "time": now(),
            "seconds": round(time.monotonic() - began),
            "slot_wait_seconds": waited,
        }
    )
    print(f"shard {shard}/{len(shards)} passed: published in origin as the record branch {ref}")
    return 0


# the subject of a queue's merge commit for one PR: Mergify's batch draft (`Merge of PR <n>`) and GitHub's merge queue
# with the merge method (`Merge pull request #<n> from …`)
QUEUE_MERGE = re.compile(r"Merge (?:of PR |pull request #)(\d+)\b")


class QueuePull(NamedTuple):
    """One PR of a queue's batch: `#<n>` (else its merge commit's first 12), its branch point (the merge base of the
    merge commit's parents) and its head (the merge commit's second parent)."""

    name: str
    point: str
    head: str


def queue_say(line: str) -> None:
    print(f"queue-select: {line}", file=sys.stderr)


def queue_pulls(base: str) -> list[QueuePull] | str:
    """Each PR of the batch checked out at HEAD, from its first-parent commits on `base..HEAD`, one merge commit per
    PR whose second parent is the PR's head; else why `base..HEAD` doesn't split into PRs (a squash or rebase queue)."""

    def soft(*args: str) -> str | None:
        try:
            return git(*args).strip()
        except subprocess.CalledProcessError:
            return None

    commits = (soft("rev-list", "--first-parent", "--reverse", f"{base}..HEAD") or "").split()
    if not commits:
        return f"no commits on {base}..HEAD"
    pulls = []
    for commit in commits:
        parents = (soft("rev-list", "--parents", "-n1", commit) or "").split()[1:]
        if len(parents) != 2:
            return f"{commit[:12]} is not a merge of one PR onto the batch"
        if (point := soft("merge-base", *parents)) is None:
            return f"{commit[:12]}'s parents have no merge base"
        number = QUEUE_MERGE.match(soft("log", "-1", "--format=%s", commit) or "")
        pulls.append(QueuePull(f"#{number[1]}" if number else commit[:12], point, parents[1]))
    return pulls


def queue_recorded(head: str, point: str) -> tuple[bool, str]:
    """Whether origin holds the pass record of loop.md's `Affected tests:` command on `head`'s tree, narrowed against
    `point` (the PR's branch point, the merge base `affected-tests --record` selects against), read as `gate-record
    <head> -- <that command>` reads it (tests_record_words: one key builder for land and the queue); and why. Any
    failure to read it is no record, said, so the PR's own tests run."""
    said = io.StringIO()
    try:
        if narrowing() is None:
            return (
                False,
                "loop.md has no `Affected tests:` line, so no record of a PR's tests exists",
            )
        if (words := tests_record_words(point, head)) is None:
            return False, "the gate has `gate_steps`, which never run the `Affected tests:` command"
        with contextlib.redirect_stdout(said):
            code = gate_record(head, words)
    except Exception as exc:  # noqa: BLE001
        return False, f"the record could not be read: {exc}"
    return code == 0, " ".join(said.getvalue().split())[:300]


def queue_unkeyed() -> list[str]:
    """The commands of the gate's steps gatekeeper.md's `select.unkeyed` lists (gate_plan): their pass is never
    reused, so every batch runs them."""
    return [step["command"] for step in gate_plan() if step["unkeyed"]]


def queue_refusal(selected: bool, missing: list[QueuePull], split: str | None = None) -> str | None:
    """Why a batch that would run nothing (`selected` false: no unkeyed check, no test) may not merge, else None: a
    PR with no record (or, where the commits don't split into PRs (`split`), no record readable at all) means a merge
    with neither a queue run that ran its selection nor a record per PR (R6)."""
    if selected:
        return None
    if split is not None:
        return f"nothing to run, and no PR's record can be read since the commits don't split into PRs ({split})"
    if missing:
        names = ", ".join(pull.name for pull in missing)
        return f"nothing to run, and {names} has no record for its head's tree: no test of it ran"
    return None


def queue_select(
    base: str,
    record: Callable[[str, str], tuple[bool, str]] | None = None,
    unkeyed: Callable[[], list[str]] | None = None,
) -> int:
    """`queue-select [<base>]`: what a merge queue's batch check runs on the batch checked out at HEAD (R6), one shell
    command per line on stdout, each to pass: the `select.unkeyed` checks, then the floor, `affected-tests --base
    <point>` for each PR with no record of its `Affected tests:` run (queue_recorded) at that PR's branch point. A PR
    with a record runs nothing: its own tests passed on its head. Where the commits don't split into PRs, the batch's
    `affected-tests --base <base>`. The floor prints with or without an `Affected tests:` line: without one,
    `affected-tests` runs gatekeeper.md's `select` cut, then the project's full check, the narrowest set it can name
    (#2143 D2, #2362). Refused (exit 1, queue_refusal) when there is nothing to run while a PR has no record; nothing
    to run with a record for every PR passes and says so. Each step's reason goes to stderr, prefixed `queue-select:`.

    ponytail: a floor runs in the batch's checkout, so it selects against everything since the PR's branch point, the
    other PRs and the base's newer commits included: a superset of the PR's own tests, run on the merged tree. A
    project with a per-test map narrows the batch to the tests two changes both reach (this repo's
    tools/cross_change_tests.py, which reads queue_pulls, queue_recorded and queue_refusal)."""
    record, unkeyed = record or queue_recorded, unkeyed or queue_unkeyed
    checks = unkeyed()
    queue_say(
        f"{len(checks)} unkeyed checks (select.unkeyed){': ' if checks else ''}{'; '.join(checks)}"
    )
    floor = [sys.executable, str(Path(__file__).resolve()), "affected-tests", "--base"]
    pulls = queue_pulls(base)
    runs: list[str] = []
    missing: list[QueuePull] = []
    if isinstance(pulls, str):
        queue_say(f"the batch's affected tests: the commits don't split into PRs ({pulls})")
        runs = [shlex.join([*floor, base])]
    else:
        for pull in pulls:
            found, line = record(pull.head, pull.point)
            queue_say(
                f"PR {pull.name} (head {pull.head[:12]}): record {'found' if found else 'none'}: {line}"
            )
            if not found:
                missing.append(pull)
                if (run := shlex.join([*floor, pull.point])) not in runs:
                    queue_say(
                        f"PR {pull.name}: no record, so its own affected tests run (the floor)"
                    )
                    runs.append(run)
    if why := queue_refusal(
        bool(checks or runs), missing, pulls if isinstance(pulls, str) else None
    ):
        queue_say(f"refused: {why}")
        return 1
    if not checks and not runs:
        queue_say("nothing to run: every PR has a record")
    print("".join(f"{line}\n" for line in [*checks, *runs]), end="")
    return 0


def failed_tests_of(path: str) -> tuple[list[str], list[str]]:
    """The test ids a red run wrote (`{"failed": [...], "selected": [...]}`, as tools/failed_tests.py writes it), its
    failed tests and the tests its report listed (none where the file names no `selected`): the run's own output,
    read before any rerun. Refused for a file that is not there or not that JSON, since a rerun before the red is read
    is never run."""
    try:
        data = json.loads(Path(path).read_text())
        lists = data["failed"], data.get("selected", [])
        for key, ids in zip(("failed", "selected"), lists):
            if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
                raise ValueError(f"`{key}` is not a list of test ids")
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise Refused(f"rerun-tests: {path} holds no red run's failed tests: {exc}") from None
    return lists


def blob_of(rev: str, name: str) -> str | None:
    """The object id of `name` at `rev`, None where it is absent there."""
    try:
        return git("rev-parse", "--verify", "--quiet", f"{rev}:{name}").strip()
    except subprocess.CalledProcessError:
        return None


def fix_since(red: str, base: str | None) -> set[str]:
    """The files the PR's own change differs in between the red run's head `red` (against the red's own base, its
    merge base with `base`) and HEAD (against its merge base now): a file the base moved and nobody on the PR touched
    is no fix, so a requeue onto a newer base is no change, whether `base` names the red's base or the newer one.
    Without a `base`, every file that differs between `red` and HEAD."""
    here = selection_base(None) if base else None
    if base is None or here is None:
        return set(git("diff", "--name-only", red, "HEAD").splitlines())
    try:
        red_base = git("merge-base", base, red).strip()
    except subprocess.CalledProcessError:
        raise Refused(
            f"rerun-tests: {red} and the base {base} share no history here: fetch the red run's head with its history"
        ) from None
    touched = set(git("diff", "--name-only", red_base, red).splitlines()) | set(
        git("diff", "--name-only", here, "HEAD").splitlines()
    )
    return {name for name in touched if blob_of(red, name) != blob_of("HEAD", name)}


def reselected(test: str, changed: set[str]) -> bool:
    """Whether a changed file is the failed test's own: the id's part before `::` is that path, or the path with
    its extension dropped and `.` for `/`, itself or followed by a class (`pkg.module.Class`)."""
    owner = test.split("::", 1)[0]
    return any(
        owner == name
        or owner == (dotted := name.rsplit(".", 1)[0].replace("/", "."))
        or owner.startswith(f"{dotted}.")
        for name in changed
    )


def rerun_plan(failed: list[str], changed: set[str]) -> tuple[bool, list[str]]:
    """What a red reruns: (True, []) when the change since the red reselects every failed test, so the rerun is those
    tests plus what the change reaches; else (False, the flake candidates), the failed tests nothing reselects, and the
    rerun is the red run's whole selection, in its order. A red with no test id (its tests never started) has
    nothing to reselect: the whole selection."""
    stuck = [test for test in failed if not reselected(test, changed)]
    return (bool(failed) and not stuck), (stuck or list(failed))


# rerun-tests' exit when the rerun ran no test (EX_TEMPFAIL, "try again"): a rerun that never started says nothing about
# the change, so it is neither a red caused by the change (the run's own exit) nor a refusal (1)
DID_NOT_RUN = 75
# the codes a POSIX shell gives a command it could not start: not executable (126), not found (127)
NOT_STARTED = (126, 127)


def did_not_run(why: str) -> None:
    """The one line of a rerun that ran no test, whose exit is DID_NOT_RUN."""
    print(
        f"rerun-tests: rerun did not run: {why}; it says nothing about the change, so the red stays unread: fix that"
        " and rerun",
        file=sys.stderr,
    )


def missing_task(text: str) -> str | None:
    """The task a `mise run <task>` selection in loop.md's `text` names that mise doesn't define here, else None: mise
    exits 1 for an unknown task, as a red test does, so after a red exit `mise tasks info` tells the two apart."""
    # ponytail: only a `mise run` line is checked; another runner's missing task reads as red again, which R5's
    # "a task the head lacks" covers too. Upgrade: ask that runner for its tasks (#2305).
    written = narrowing(text)
    words = command_line(written, "Affected tests")[1] if written else [*LOCAL_CI[:-1], "check"]
    if words[:2] != ["mise", "run"] or len(words) < 3:
        return None
    try:
        known = subprocess.run(
            ["mise", "tasks", "info", words[2]], capture_output=True, cwd=toplevel(), check=False
        )
    except OSError:
        return words[2]
    return None if known.returncode == 0 else words[2]


def tests_in(report: Path) -> int:
    """How many test cases the JUnit XML `report` holds; 0 for one that is missing or unreadable, since a run that
    wrote no report ran no test it can show."""
    try:
        return sum(1 for _ in ET.parse(report).getroot().iter("testcase"))
    except (OSError, ET.ParseError):
        return 0


def rerun(red: str, base: str | None, text: str, red_ran: bool) -> int | None:
    """The rerun's selection against `base` with the red head's loop.md `text`: its exit, or None when it ran no test
    (no base, a command that can't start, a `mise run` task that isn't there, or a `Test report:` that shows zero tests
    where the red run ran some, `red_ran`), having said why (did_not_run). The report is removed before the run, so a
    report an earlier run left is never counted as this one's. Zero tests counts only for a green exit or a report the
    run wrote: a red exit with no report failed before its tests ran (a lint step, a check ahead of the runner), and
    that is red again, as any red rerun is."""
    if base is None:
        return did_not_run("no base to select against")
    found = TEST_REPORT.search(text)
    report = toplevel() / found.group(1) if found else None
    if report:
        report.unlink(missing_ok=True)
    try:
        code = affected_tests(base, text=text)
    except Refused as exc:
        return did_not_run(str(exc))
    if code in NOT_STARTED:
        return did_not_run(f"the selection command could not start (exit {code})")
    if code and (task := missing_task(text)):
        return did_not_run(
            f"{red}'s loop.md runs `mise run {task}`, a task mise doesn't define here"
        )
    if report and red_ran and (code == 0 or report.exists()) and not tests_in(report):
        return did_not_run("zero tests collected")
    return code


def rerun_tests(path: str, red: str, given: str | None) -> int:
    """`rerun-tests <failed.json> --red <head> [--base <rev>]`: the one rerun of a red run, from the red run's own
    output (failed_tests_of), its head `red` and its base (`--base`, else the merge base now), recomputing the
    selection from those inputs: nothing is stored. Under the project's `Affected tests:` line (affected_tests),
    which gives the same selection in the same order on the same base:
    - the change since the red reselects every failed test (rerun_plan): the selection against `red`, the failed tests
      plus what the change reaches;
    - otherwise (nothing changed, or a change that misses a failed test): the selection against HEAD's own base now,
      the red run's whole selection (after a rebase, its PR files on the newer base, not the files the base moved). A failed test that passes there is a flake (a `Flaky:` issue, the run counted green),
      and one that is red again stays red: the change caused it. Never the failed tests alone, and never the whole
      suite. One line says which and why; the exit is the run's.
    The selection is the red run's own, so its command comes from the red head's loop.md, never the base's: a head
    behind its base lacks a task the base's line names (ponytail: the R1 record would name the command too, but only a
    pass writes one, so a red head never has one; read it first if a red run ever records). A rerun that ran no test
    (rerun), its `Test report:` showing zero tests where the red had some included, exits DID_NOT_RUN, never as red
    again or as a flake that passed."""
    failed, selected = failed_tests_of(path)
    red_ran = bool(failed or selected)
    try:
        git("rev-parse", "--verify", "--quiet", f"{red}^{{commit}}")
    except subprocess.CalledProcessError:
        raise Refused(f"rerun-tests: no commit {red} here: fetch the red run's head") from None
    base = selection_base(given)
    text = (shown_at(toplevel(), red, LOOP) or b"").decode()
    if narrowing(text) is None and not FULL_GATE.search(text):
        did_not_run(f"{red}'s loop.md names no `Affected tests:` selector")
        return DID_NOT_RUN
    fixed, candidates = rerun_plan(failed, fix_since(red, base))
    # HEAD's own base now, past any rebase: the whole selection is the PR's files, not the ones the base moved
    here = selection_base(None) if given is not None else base
    if fixed:
        print(
            f"rerun-tests: the change since the red reselects {len(failed)} failed test(s), so those and what it"
            " reaches run",
            file=sys.stderr,
        )
        code = rerun(red, red, text, red_ran)
        return DID_NOT_RUN if code is None else code
    for test in candidates:
        print(f"rerun-tests: flake candidate: {test}", file=sys.stderr)
    print(
        f"rerun-tests: nothing changed reselects {len(candidates)} failed test(s), so the red run's whole selection"
        " runs: alone, a test that fails only after others would pass",
        file=sys.stderr,
    )
    code = rerun(red, here or base, text, red_ran)
    if code is None:
        return DID_NOT_RUN
    if code == 0 and candidates:
        print(
            "rerun-tests: green: a flake candidate that passed is a flake: open or update its `Flaky:` issue and"
            " count the run green",
            file=sys.stderr,
        )
    elif code:
        print("rerun-tests: red again on the same selection: the change caused it", file=sys.stderr)
    return code


def ran_step(
    step: dict[str, Any],
    cwd: Path | None = None,
    out: Any = None,
    load: dict[str, Any] | None = None,
    land: bool = False,
    beat: Callable[[], None] | None = None,
) -> tuple[int, int, int]:
    """Run one gate step (gate_plan) in its environment (gate_env) under the host gate slot (heavy_slot), in `cwd`
    with its output to `out` when given: its exit, the seconds it ran and the seconds it waited for the slot; `load`,
    when given, gets the host's load at the step's start, once it holds the slot (host_load). A command that isn't
    there is refused, naming the gate, since loop.md is hand-written. `beat`, when given, is called while the step waits
    for the slot (heavy_slot) and while it runs, every SLOT_SAY_SECONDS (the gate event, gate_beat)."""
    where = {"cwd": cwd, "stdout": out, "stderr": subprocess.STDOUT} if cwd else {}
    with heavy_slot(step["name"], out, land, **({"beat": beat} if beat else {})) as (slot, waited):
        if load is not None:
            load.update(host_load())
        began = time.monotonic()
        try:
            with repeating(beat) if beat else contextlib.nullcontext():
                if beat:
                    beat()
                code = run_heavy(step["words"], env=gate_env(step) | {SLOT_HELD: slot}, **where)[0]
        except OSError as exc:
            what = "gate" if step["legacy"] else f"gate step `{step['name']}`"
            raise Refused(f"loop.md's {what} {step['command']!r} can't run: {exc}") from None
        return code, round(time.monotonic() - began), waited


def host_load() -> dict[str, Any]:
    """The host's 1-minute load average and the swap it uses, in whole MiB, each None where the host doesn't say, so a
    step's seconds can be read beside the load it ran under (#963)."""
    try:
        load: float | None = round(os.getloadavg()[0], 2)
    except OSError:
        load = None
    return {"load_1m": load, "swap_used_mb": swap_used_mb()}


MEMINFO = "/proc/meminfo"


def swap_used_mb() -> int | None:
    """Swap in use, in whole MiB: Linux's /proc/meminfo, else macOS's `sysctl vm.swapusage`; None for neither."""
    with contextlib.suppress(OSError, KeyError, ValueError, IndexError):
        with open(MEMINFO) as info:
            kib = {
                name: int(rest.split()[0])
                for name, _, rest in (line.partition(":") for line in info)
                if name in ("SwapTotal", "SwapFree")
            }
        return (kib["SwapTotal"] - kib["SwapFree"]) // 1024
    with contextlib.suppress(OSError, subprocess.SubprocessError, ValueError):
        said = subprocess.run(
            ["sysctl", "-n", "vm.swapusage"], capture_output=True, text=True, check=False
        ).stdout
        if used := re.search(r"used = ([\d.]+)M", said):
            return int(float(used[1]))
    return None


def gate_env(step: dict[str, Any]) -> dict[str, str]:
    """The environment a gate step runs in: this process's, without the names `local_ci_env` lists and without the
    `Gate base:` variable (gate_base_variable), unless the step's command sets them, or a head proof's `passed` takes
    them from this process's (gate): a caller's base never narrows a plain pass."""
    # the tier's names too (tier): a shell's own `GATE_INCUBATING` would run a reduced gate whose pass is keyed as a full one
    cleared = set(local_ci_env()) | {"GATE_BASE", *INCUBATING_ENV}
    with contextlib.suppress(Refused, OSError):  # the working tree's loop.md, read with no git call
        held = toplevel() / LOOP
        named = (
            re.search(r"^Gate base:[ \t]*`?([A-Za-z_]\w*)\b", held.read_text(), re.MULTILINE)
            if held.exists()
            else None
        )
        cleared |= {named.group(1)} if named else set()
    return {name: value for name, value in os.environ.items() if name not in cleared} | step["sets"]


# the variable naming the host gate slot a run holds, so a gate wrapper under it takes no lock of its own (SKILL.md, One
# heavy gate per host slot; mutate.py sets it the same way)
SLOT_HELD = "LOOP_GATE_SLOT_HELD"


def slot_file(name: str) -> str:
    """A lock file of the host's gate, by name, in the cache every worktree and clone of the host shares."""
    cache = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.environ.get("HOME", "~"), ".cache")
    return os.path.join(cache, "engineering-loop", name)


# loop.md's line stating what one heavy gate run costs, measured on the project's heaviest gate (SKILL.md, One heavy
# gate per host slot): `Gate cost: <cores> cores, <GB> GB`, optionally `, reserve <GB> GB`
GATE_COST = re.compile(
    r"^Gate cost:[ \t]*`?(?P<cores>\d+(?:\.\d+)?) cores?,[ \t]*(?P<gb>\d+(?:\.\d+)?) GB"
    r"(?:,[ \t]*reserve (?P<reserve>\d+(?:\.\d+)?) GB)?`?[ \t]*$"
)
GIB = 1 << 30


def slot_rule(
    cores: int,
    memory: Fraction | None,
    cost: tuple[Fraction, Fraction] | None,
    reserve: Fraction | None,
    said: str,
) -> tuple[int, str, Fraction | None]:
    """The host's heavy slot count, the limit that set it (`off`, `GATE_SLOTS`, `cores` or `memory`) and the reserve
    it used, from the host's cores and total memory in GB (None when unread), the gate's cost per run (cores, GB; None
    when loop.md states none), loop.md's reserve (None for the default) and `GATE_SLOTS`. `off` is 0 slots: every run
    goes with no lock. A whole number from 1 is the count. Otherwise the smaller of cores ÷ cost cores and (memory −
    reserve) ÷ cost GB, at least 1, a tie being the cores'; with no cost, a third of the cores, at least 1. The reserve
    defaults to a quarter of the memory, at least 4 GB. The template's `gate-slot` follows the same rule, and the
    table in tests/fixtures/gate-slots.tsv holds both to it."""
    if reserve is None and memory is not None:
        reserve = max(Fraction(4), memory / 4)
    if said == "off":
        return 0, "off", reserve
    if said.isdigit() and int(said) >= 1:
        return int(said), "GATE_SLOTS", reserve
    if cost is None:
        return max(1, cores // 3), "cores", reserve
    by_cores = int(cores // cost[0])
    if memory is None or reserve is None:
        return max(1, by_cores), "cores", reserve
    by_memory = int((memory - reserve) // cost[1])
    if by_memory < by_cores:
        return max(1, by_memory), "memory", reserve
    return max(1, by_cores), "cores", reserve


def total_memory() -> Fraction | None:
    """The host's total memory in whole GB (GiB, rounded), never its available memory, so the count stays put between
    runs and a waiter never waits on a slot that no longer exists: page size × physical pages from `os.sysconf`, which
    macOS and Linux both answer and which runs no command, so a test's stand-in for `subprocess.run` is never asked;
    None where it is not known."""
    with contextlib.suppress(OSError, ValueError, AttributeError):
        found = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        return Fraction((found + GIB // 2) // GIB) if found > 0 else None
    return None


def gate_cost() -> tuple[tuple[Fraction, Fraction] | None, Fraction | None]:
    """loop.md's `Gate cost:` line (GATE_COST) in the checkout this runs in, as read from its root as the template's
    `gate-slot` greps it: ((cores, GB), reserve or None), or (None, None) outside a repo or without the line. The root
    is the nearest directory up from here holding `.git`, found without running git, since a heavy step's caller may
    run anywhere and this is read on every slot it takes. A line off its form, or a cost of 0, is refused, naming the
    line, since a stated cost ignored would oversubscribe the host."""
    here = Path.cwd()
    top = next((d for d in (here, *here.parents) if (d / ".git").exists()), None)
    with contextlib.suppress(OSError):
        text = "" if top is None else (top / LOOP).read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            if not line.startswith("Gate cost:"):
                continue
            found = GATE_COST.match(line.rstrip())
            if found is None or not Fraction(found["cores"]) or not Fraction(found["gb"]):
                raise Refused(
                    f"loop.md's {line.strip()!r} is not `Gate cost: <cores> cores, <GB> GB`"
                    " (optional `, reserve <GB> GB`), each cost above 0"
                )
            reserve = found["reserve"]
            return (Fraction(found["cores"]), Fraction(found["gb"])), (
                None if reserve is None else Fraction(reserve)
            )
    return None, None


def plan_number(value: Fraction | None) -> str:
    """A count of cores or GB as the plan line prints it: `4`, `1.5`, `?` when unread."""
    return "?" if value is None else f"{float(value):g}"


def slot_plan() -> tuple[int, str]:
    """The host's heavy slot count (slot_rule on this host) and the line saying it, the same as the template's
    `gate-slot --plan`: `slots <n>: <limit> (<cores> cores, <GB> GB, cost <c> cores <m> GB, reserve <r> GB)`, with
    `cost none` where loop.md states no cost."""
    cores = os.cpu_count() or 1
    memory = total_memory()
    cost, reserve = gate_cost()
    count, limit, reserve = slot_rule(
        cores, memory, cost, reserve, os.environ.get("GATE_SLOTS", "")
    )
    stated = "none" if cost is None else f"{plan_number(cost[0])} cores {plan_number(cost[1])} GB"
    return count, (
        f"slots {count}: {limit} ({cores} cores, {plan_number(memory)} GB, cost {stated},"
        f" reserve {plan_number(reserve)} GB)"
    )


def gate_slots() -> int:
    """How many heavy gates the host runs at once (slot_plan), 0 under `GATE_SLOTS=off`, when every run goes with no
    lock. A runner under a slot takes at most the host's cores divided by this number as its `GATE_JOBS`, since this
    many gates load the host at once and a larger share would oversubscribe it."""
    return slot_plan()[0]


def slots_off() -> bool:
    """Whether `GATE_SLOTS=off` turns the host gate off: every heavy run, the short lane's too, goes with no lock."""
    return os.environ.get("GATE_SLOTS") == "off"


def slot_path(number: int = 1) -> str:
    """The lock file of heavy gate slot `number` (1..gate_slots()), the one SKILL.md, mutate.py and the project's gate
    wrapper name."""
    return slot_file(f"gate.{number}.lock")


def held_slot() -> str | None:
    """The heavy slot this process already runs under (SLOT_HELD names its lock file), else None: any `gate.<n>.lock`
    of the slot directory, whatever `GATE_SLOTS` this process reads, since its parent may have taken a slot under
    another count."""
    held = os.environ.get(SLOT_HELD) or ""
    same = os.path.dirname(held) == os.path.dirname(slot_path())
    return held if same and re.fullmatch(r"gate\.\d+\.lock", os.path.basename(held)) else None


def land_waiting() -> bool:
    """Whether a land's gate step waits for a slot on this host (take_slot's `land`): it holds a shared flock on the
    `gate.land.lock` file while it waits, so an exclusive try on that file fails exactly then."""
    fd = os.open(slot_file("gate.land.lock"), os.O_WRONLY | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)
    return False


def take_slot(
    waiting: Callable[[], None] = lambda: None, land: bool = False
) -> tuple[int | None, str]:
    """Hold the first free heavy slot's exclusive flock and return (its fd, its path); closing the fd frees the lock,
    as does the process dying. A run already under a holder of a slot (held_slot) takes none: (None, that path), since
    it would wait on its own parent. Under `GATE_SLOTS=off` it takes none either: (None, ""). With every slot held, `waiting` is called, again every SLOT_SAY_SECONDS, and the slots
    are tried again until one frees. A land's gate step (`land`) holds the landing lock of its base while it waits, which blocks every other
    land, so it takes a freed slot ahead of any PR gate: flock has no priority, so a waiting land marks itself (a shared
    flock on `gate.land.lock`, held only while it waits) and a PR gate that finds the mark takes no slot, trying again
    until no land waits."""
    if (held := held_slot()) is not None:
        return None, held
    count = gate_slots()
    if not count:  # GATE_SLOTS=off: no lock, and no slot named to the run
        return None, ""
    os.makedirs(os.path.dirname(slot_path()), exist_ok=True)
    mark = os.open(slot_file("gate.land.lock"), os.O_WRONLY | os.O_CREAT, 0o644) if land else None
    if mark is not None:
        fcntl.flock(mark, fcntl.LOCK_SH)
    said = None  # when the wait is next said
    try:
        if land and count == 1:
            # one slot: block in the kernel, as a gate wrapper's own flock does, so a land competes evenly with a
            # wrapper waiter instead of losing every race to it by polling; PR gates poll and yield to the mark
            fd = os.open(slot_path(), os.O_WRONLY | os.O_CREAT, 0o644)
            try:
                flock_saying(fd, waiting)
            except BaseException:
                os.close(fd)
                raise
            return fd, slot_path()
        while True:
            if land or not land_waiting():
                for slot in range(1, count + 1):
                    path = slot_path(slot)
                    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o644)
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        os.close(fd)
                        continue
                    except BaseException:
                        os.close(fd)
                        raise
                    return fd, path
            if said is None or time.monotonic() >= said:
                said = time.monotonic() + SLOT_SAY_SECONDS
                waiting()
            time.sleep(0.2)
    finally:
        if mark is not None:
            os.close(mark)


# the heavy wrapper (SKILL.md, One heavy gate per host slot): a command prefix a host sets in a file outside every repo,
# which every heavy step runs under; a wrapped step carries the variable, so a heavy call under it adds no second one
HEAVY_WRAPPED = "LOOP_HEAVY_WRAPPED"
# how long `<prefix> true` may take before the wrapper counts as unavailable
HEAVY_PROBE_SECONDS = 30
# each prefix probed in this process: why it can't run, or None where it can, so its probe and line come once
heavy_probed: dict[tuple[str, ...], str | None] = {}


def host_config_path() -> str:
    """The host config file (`key = value` lines), beside the gate slot's lock under the config home, not the cache."""
    config = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.environ.get("HOME", "~"), ".config"
    )
    return os.path.join(config, "engineering-loop", "host")


def host_config() -> dict[str, str]:
    """The host config's `key = value` lines (blank lines and `#` comments skipped; the first line of a key wins), or
    nothing where the file is absent."""
    try:
        with open(host_config_path(), encoding="utf-8", errors="replace") as text:
            pairs = [s.partition("=") for s in map(str.strip, text) if s and not s.startswith("#")]
    except OSError:
        return {}
    config: dict[str, str] = {}
    for key, _, value in pairs:
        config.setdefault(key.strip(), value.strip())
    return config


def heavy_prefix(env: Any = None) -> list[str]:
    """The heavy wrapper's words: the value of the host config's first `heavy_wrapper = <prefix>` line (blank lines,
    `#` comments and other keys are skipped), split like a shell command; none without the file or such a line, under a wrapped step (HEAVY_WRAPPED in `env`,
    else this process's), or where the prefix can't run, which says so on stderr once per process and runs the step unwrapped, since the
    gate must never fail because of the wrapper."""
    if (os.environ if env is None else env).get(HEAVY_WRAPPED) == "1":
        return []
    line = host_config().get("heavy_wrapper", "")
    if not line:
        return []
    try:
        words = shlex.split(line)
    except ValueError as exc:
        words, why = [line], f"its line can't be split ({exc})"
        heavy_probed.setdefault(tuple(words), why)
    key = tuple(words)
    if key not in heavy_probed:
        heavy_probed[key] = heavy_unusable(words)
        if heavy_probed[key]:
            print(
                f"heavy wrapper unavailable: {heavy_probed[key]}; running unwrapped",
                file=sys.stderr,
                flush=True,
            )
    return [] if heavy_probed[key] else words


def heavy_unusable(words: list[str]) -> str | None:
    """Why the prefix `words` can't run a step: its command is not on PATH, or `<prefix> true` fails or outlasts
    HEAVY_PROBE_SECONDS (a bad property makes systemd-run exit before it runs anything); None when it can."""
    if not words or not shutil.which(words[0]):
        return f"`{words[0] if words else ''}` is not on PATH"
    try:
        probe = subprocess.Popen(
            [*words, "true"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        return f"`{shlex.join(words)} true` can't run: {exc}"
    try:
        code = probe.wait(timeout=HEAVY_PROBE_SECONDS)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            os.killpg(probe.pid, signal.SIGKILL)
        probe.wait()
        return f"`{shlex.join(words)} true` took over {HEAVY_PROBE_SECONDS} s"
    return f"`{shlex.join(words)} true` exited {code}" if code else None


def heavy_argv(words: Sequence[str], env: Any = None) -> list[str]:
    """A heavy step's argv: `words` behind the host's heavy wrapper (heavy_prefix), or `words` unchanged."""
    return [*heavy_prefix(env), *words]


def stop_exit(signum: int, _frame: Any) -> None:
    """A stop that nothing handled ends the run as the default would, but as an exception, so run_heavy kills the
    step's group first. Further stops are ignored, since `uv run` forwards the same signal again and a second exit
    would cut that kill short."""
    for sig in STOPS:
        signal.signal(sig, signal.SIG_IGN)
    sys.exit(128 + signum)


@contextlib.contextmanager
def stops_raise() -> Any:
    """While a heavy step runs, SIGTERM and SIGHUP still on their default handler raise SystemExit (stop_exit); one
    ignored (nohup) or handled in Python (land's `stopped`, mutate.py's) is left alone, and each is put back after.
    Outside the main thread no handler can be set, so none is."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = {}
    for sig in STOPS:
        if signal.getsignal(sig) is signal.SIG_DFL:
            previous[sig] = signal.signal(sig, stop_exit)
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


HOST_COMMANDS = "~/.agents/host-commands"
# what the Claude Code hook that reads the same file strips before it matches: launchers, wrappers, shells
LAUNCHERS = (["swiftly", "run"], ["uv", "run"], ["python3"], ["python"])
WRAPPERS = {
    "env",
    "exec",
    "command",
    "nohup",
    "time",
    "sudo",
    "nice",
    "setsid",
    "timeout",
    "xargs",
    "stdbuf",
}
SHELLS = {"sh", "bash", "zsh", "dash"}


def host_forbids(words: Sequence[str]) -> str | None:
    """The line of the host's policy file (HOST_COMMANDS, or the file `AGENT_HOST_COMMANDS` names; one command per
    line, text after `#` is a comment) whose words begin `words`, as the Claude Code hook that reads the same file
    matches a typed command: path words by their basename, `VAR=` words and wrappers (WRAPPERS, with their options and
    a count) dropped, then launchers (LAUNCHERS) stripped as often as they lead. A shell's `-c <script>` is read
    segment by segment (`&&`, `;`, `|`). None where the file is absent or lists no such command, so a host without a
    policy runs everything."""
    path = os.path.expanduser(os.environ.get("AGENT_HOST_COMMANDS") or HOST_COMMANDS)
    try:
        with open(path, encoding="utf-8", errors="replace") as text:
            rules = [r for r in (line.split("#")[0].split() for line in text) if r]
    except OSError:
        return None

    def forbidden(command: list[str]) -> str | None:
        command = [os.path.basename(w) if "/" in w else w for w in command]
        while command and (re.match(r"\w+=", command[0]) or command[0] in WRAPPERS):
            wrapper = command[0] in WRAPPERS
            command = command[1:]
            while (
                wrapper
                and command
                and (command[0].startswith("-") or re.fullmatch(r"[\d.]+[smhd]?", command[0]))
            ):
                command = command[1:]
        if len(command) > 2 and command[0] in SHELLS and re.fullmatch(r"-\w*c\w*", command[1]):
            lexer = shlex.shlex(command[2], posix=True, punctuation_chars=True)
            lexer.whitespace_split, lexer.commenters = True, ""
            try:
                parts = list(lexer)
            except ValueError:
                parts = []
            segments, now = [], []
            for part in parts:
                if set(part) <= set("();<>|&"):
                    segments, now = [*segments, now], []
                else:
                    now.append(part)
            return next((hit for seg in [*segments, now] if seg and (hit := forbidden(seg))), None)
        while launcher := next((x for x in LAUNCHERS if command[: len(x)] == x), None):
            command = command[len(launcher) :]
        return next((" ".join(r) for r in rules if command[: len(r)] == r), None)

    return forbidden(list(words))


def run_heavy(
    words: Sequence[str], env: Any = None, budget: float | None = None, **popen: Any
) -> tuple[int, Any]:
    """Run one heavy step (a gate step, a `pre_land` check, a mutate.py test run, `heavy`) under the host's heavy
    wrapper (heavy_argv), with `popen` as Popen's other arguments: its exit code and what communicate() read from its
    stdout (None unless piped). With `budget`, a step still running after that many seconds has its process group
    killed and exits TIMED_OUT with SPENT for its output, a timeout and no test's failure. It runs in a session of its own, and on any exception or stop while it runs its whole
    process group is killed before the exception goes on, so neither the step nor what it started outlives a stopped
    run: a wrapper (systemd-run --scope) puts the step in a group of its own, which a stop of the caller's group would
    never reach. A wrapped step's environment carries HEAVY_WRAPPED."""
    if rule := host_forbids(words):
        raise Refused(
            f"no gate record for this tree: `{shlex.join(words)}` is on this host's {HOST_COMMANDS} ({rule!r}): "
            "gate it on an allowed host or in claude-cloud"
        )
    prefix = heavy_prefix(env)
    if prefix:
        env = {**(os.environ if env is None else env), HEAVY_WRAPPED: "1"}
    with stops_raise():
        step = None
        try:
            step = subprocess.Popen([*prefix, *words], env=env, start_new_session=True, **popen)
            try:
                output, _ = step.communicate(timeout=budget)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(OSError):
                    os.killpg(step.pid, signal.SIGKILL)
                step.wait()
                return TIMED_OUT, SPENT
        except BaseException:
            if step is not None:
                with contextlib.suppress(OSError):
                    os.killpg(step.pid, signal.SIGKILL)
                step.wait()
            raise
    return step.returncode, output


# how often a run queued on a host gate slot says it still waits: wait-for kills a run whose log is silent for 300 s
SLOT_SAY_SECONDS = 60.0


def flock_saying(fd: int, said: Callable[[], None]) -> None:
    """Take fd's exclusive flock, blocking in the kernel. A wait calls `said` when it begins and again every
    SLOT_SAY_SECONDS, so a log that shows the wait never goes silent long enough for wait-for's stall check to read
    the queue as a hang."""
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        said()
        with repeating(said):
            fcntl.flock(fd, fcntl.LOCK_EX)


@contextlib.contextmanager
def repeating(call: Callable[[], None]) -> Any:
    """Call `call` every SLOT_SAY_SECONDS from a thread of its own while the block runs."""
    stop = threading.Event()

    def again() -> None:
        while not stop.wait(SLOT_SAY_SECONDS):
            call()

    caller = threading.Thread(target=again, daemon=True)
    caller.start()
    try:
        yield
    finally:
        stop.set()
        caller.join()


def saying(text: str) -> Any:
    """Print `text` every SLOT_SAY_SECONDS while the block runs, to the process's own stdout, since `land --wait`
    captures sys.stdout until landing returns: a run whose output goes to a log of its own (a batch gate) then never
    reads as a hang to wait-for's stall check."""
    return repeating(lambda: print(text, file=sys.__stdout__, flush=True))


@contextlib.contextmanager
def heavy_slot(
    name: str, out: Any = None, land: bool = False, beat: Callable[[], None] | None = None
) -> Any:
    """Hold a host gate slot (take_slot, `land` as it takes it) for one gate step: (its path, the whole seconds
    waited for it). A wait is said as `local-ci: <step>: waiting for the host gate slot: <slot_plan's line>`, again
    every SLOT_SAY_SECONDS, so the log shows how many slots the host has and which limit set that. `beat`, when given,
    is called at each of those, so the gate event is rewritten while the step waits."""
    began = time.monotonic()

    def say() -> None:
        print(
            f"local-ci: {name}: waiting for the host gate slot: {slot_plan()[1]}",
            file=out or sys.stderr,
            flush=True,
        )
        if beat:
            beat()

    fd, path = take_slot(say, land)
    try:
        yield path, 0 if fd is None else round(time.monotonic() - began)
    finally:
        if fd is not None:
            os.close(fd)


def local_ci_dir() -> Path:
    """Where local-ci keeps its records and runs: the git common dir, which every worktree of the clone shares and no
    commit carries."""
    common = Path(git("rev-parse", "--path-format=absolute", "--git-common-dir").strip())
    return common / "approvals" / "local-ci"


def recorded(tree: str, command: str) -> Path:
    """Where a local-ci pass of `command` on `tree` is recorded."""
    return local_ci_dir() / f"{tree}-{gate_key(command)}.json"


def gate_key(command: str) -> str:
    """A digest of the gate command, which names a record and stands for a command too long to publish (described)."""
    return hashlib.sha256(command.encode()).hexdigest()[:12]


def described(tree: str, commit: str, command: str) -> str:
    """A published pass's status description: the tree, the commit it ran on, and the command, or its digest when the
    command doesn't fit, so another host reads what ran and a refusal can name it; a narrowed pass's (narrowed_gate)
    says `narrowed vs <base's first 12>` first."""
    said = command
    if (base := narrowed_base(command)) is not None:
        # the base is said once, by its first 12, so the command after it fits more often
        said = f"narrowed vs {base[:12]}: {shlex.join(shlex.split(command[: -len(NARROWED)])[1:])}"
    line = f"Tree {tree} on {commit[:12]}: {said}"
    if len(line) <= DESCRIBED:
        return line
    narrowed = f"narrowed vs {base[:12]}, " if base is not None else ""
    return f"Tree {tree} on {commit[:12]}: {narrowed}sha256 {gate_key(command)}"


# how long tree_of waits for one commit's fetch, so a check never hangs on the network
TREE_FETCH_SECONDS = 60
# each (checkout, commit) tree_of already asked origin for in this run, so a missing one costs one fetch, not one per call
TREE_FETCHED: set[tuple[str, str]] = set()


def tree_of(commit: str) -> str | None:
    """The tree `commit` holds, else None. A commit the clone lacks is fetched from `origin` by itself (GitHub serves
    any commit reachable from a branch or a PR), once per run and within TREE_FETCH_SECONDS, since a bare fetch brings
    every branch and may wait on the network for ever; a failed or timed-out fetch is None."""
    with contextlib.suppress(subprocess.CalledProcessError):
        return git("rev-parse", f"{commit}^{{tree}}").strip()
    asked = (os.getcwd(), commit)
    if asked in TREE_FETCHED:
        return None
    TREE_FETCHED.add(asked)
    fetch = ["git", *SIGNING, "fetch", "--quiet", "--no-tags", "origin", commit]
    try:
        # its own process group with no pipes, so a timeout kills the transport (ssh, a credential helper) too
        run = subprocess.Popen(
            fetch, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True
        )
    except OSError:
        return None
    try:
        if run.wait(timeout=TREE_FETCH_SECONDS):
            return None
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(run.pid, signal.SIGKILL)
        run.wait()
        return None
    with contextlib.suppress(subprocess.CalledProcessError):
        return git("rev-parse", f"{commit}^{{tree}}").strip()
    return None


def checkout_state() -> tuple[str, str]:
    """HEAD and every change in the checkout, empty when it is clean."""
    # untracked files and submodule edits listed whatever status.showUntrackedFiles or submodule.*.ignore say
    status = git("status", "--porcelain", "--untracked-files=all", "--ignore-submodules=none")
    # status skips files whose index flag says so (assume-unchanged, set by core.ignoreStat; skip-worktree)
    hidden = [
        f"{line[2:]} (index flag hides its changes)\n"
        for line in git("ls-files", "-v").splitlines()
        if line[:1].islower() or line[:1] == "S"
    ]
    return git("rev-parse", "HEAD").strip(), status + "".join(hidden)


def base_branch(pr: int | None) -> str | None:
    """The branch `local-ci` diffs against: PR `pr`'s base, else the default branch (`origin/HEAD`, else what `origin`
    says its HEAD is); None when neither answers."""
    if pr is not None:
        return str(pull_status(pr)["baseRefName"])
    with contextlib.suppress(subprocess.CalledProcessError):
        return (
            git("symbolic-ref", "--short", "refs/remotes/origin/HEAD")
            .strip()
            .removeprefix("origin/")
        )
    with contextlib.suppress(subprocess.CalledProcessError):
        for line in git("ls-remote", "--symref", "origin", "HEAD").splitlines():
            if line.startswith("ref: refs/heads/"):
                return line.split("\t")[0].removeprefix("ref: refs/heads/")
    return None


DEEPEN_STEPS = 3
DEEPEN_BY = 50


def deepened(pr: int | None) -> None:
    """In a shallow clone, fetch the base (base_branch) into `refs/remotes/origin/<base>` and deepen until `git
    merge-base origin/<base> HEAD` answers (`--deepen 50` up to DEEPEN_STEPS times, then `--unshallow`), so a gate
    step that diffs against the merge base gets a ref; each step's outcome is a line on stderr. A full clone runs
    nothing, and no tracked file changes, so the tree a pass records is the same."""
    if git("rev-parse", "--is-shallow-repository").strip() != "true":
        return
    # a `local-ci <pr>` resolved its base already (policy_from)
    base = POLICY_BASE.get() or base_branch(pr)
    if base is None:
        print(
            "local-ci: shallow clone, but no base branch is known: not deepening", file=sys.stderr
        )
        return
    ref = f"origin/{base}"
    spec = f"+refs/heads/{base}:refs/remotes/{ref}"

    def fetch(*flags: str) -> bool:
        done = subprocess.run(
            ["git", "fetch", "--quiet", *flags, "origin", spec],
            capture_output=True,
            text=True,
            check=False,
        )
        what = " ".join(flags) or "fetch"
        outcome = (
            "ok" if done.returncode == 0 else f"failed: {done.stderr.strip() or done.returncode}"
        )
        print(
            f"local-ci: shallow clone, git fetch {what} origin {base}: {outcome}", file=sys.stderr
        )
        return done.returncode == 0

    def found() -> bool:
        return (
            subprocess.run(
                ["git", "merge-base", ref, "HEAD"], capture_output=True, check=False
            ).returncode
            == 0
        )

    if fetch() and found():
        return
    for _ in range(DEEPEN_STEPS):
        if fetch("--deepen", str(DEEPEN_BY)) and found():
            return
    if fetch("--unshallow") and found():
        return
    print(f"local-ci: no merge base of {ref} and HEAD after deepening", file=sys.stderr)


loop_events = sibling("events")


def gate_branch() -> str | None:
    """The branch the gate gates, which names its `gate` event (events.md): the checked-out one, since a run is on the
    head it gates; none for a detached head, which has no event."""
    # ponytail: not the PR's head branch (a REST read `local-ci <pr>` does not make otherwise), so a checkout on another
    # local name than the PR's branch writes the event under its own
    # a run of named steps (`--step`) is no run of the gate, so it ends no gate event
    if ASKED.get():
        return None
    with contextlib.suppress(Exception):
        return git("symbolic-ref", "-q", "--short", "HEAD").strip() or None
    return None


def gate_url(record: dict[str, Any]) -> str:
    """Where a pass is recorded, as a gate event's `url`: the record file, or with `gate_steps` the records' directory."""
    return str(local_ci_dir() if "steps" in record else recorded(record["tree"], record["command"]))


def gate_event(branch: str | None, state: str, url: str | Callable[[], str] = "") -> None:
    """Write the `gate` event of `branch` (events.emit). It reports the gate and is no part of it, so whatever goes wrong
    in writing it is dropped and the gate's exit stays its own. `url` may be a function, called inside that guard, so a
    record that lacks a field costs the event and nothing else."""
    if branch is None:
        return
    if state != "waiting":
        ENDED["state"] = state
    # ponytail: emit's one "not pushed" warning is dropped here, since publish() already says when origin is unreachable
    with contextlib.suppress(Exception), contextlib.redirect_stderr(io.StringIO()):
        loop_events.emit("gate", branch, state, url=url() if callable(url) else url)


def run_local_ci(
    pr: int | None, head: str | None, passed: list[str] | tuple[str, ...] = ()
) -> dict[str, Any]:
    """Run the gate (with `passed`, gate) on a clean checkout (at PR `pr`'s `head`, when given) and record the pass by
    tree and command, one record per step (run_steps), each step with a pass on the tree reused; refuse a check off
    the head, on a changed tree, failing, or changing the tree, recording nothing for the step that did. The pass
    (whole): with a `host` step left in a cloud session, its `left` names it, and it is no pass on the whole gate."""
    before = checkout_state()
    if head and before[0] != head:
        raise Refused(
            f"the checkout is at {before[0]}, PR #{pr}'s head is {head}: push, or check out the head"
        )
    if before[1]:
        raise Refused(f"the tree has changes, so the check would not run on the head:\n{before[1]}")
    deepened(pr)
    tree = git("rev-parse", "HEAD^{tree}").strip()

    def changes() -> str:
        now = checkout_state()
        return head_moved(before[0], tree, now[0], git("rev-parse", "HEAD^{tree}").strip()) or (
            "" if now == before else now[1]
        )

    branch = gate_branch()
    try:
        done = run_steps(
            before[0], tree, changes, passed=passed, beat=lambda: gate_event(branch, "waiting")
        )
    except BaseException:
        # a refusal or an interrupt ends the run too: a reader must not find `waiting` left to go silent
        gate_event(branch, "failed")
        raise
    code, step = done["code"], done["step"]
    if step is not None:
        command = step["command"]
        # a failed step records nothing, so its url is the detached run's log when there is one, else empty
        log = run_files(before[0])[0]
        gate_event(branch, "failed", str(log) if log.exists() else "")
        nothing = "nothing recorded" if step["legacy"] else f"nothing recorded for `{step['name']}`"
        machine = code == settings(project()[1]).get("machine_exit")
        # the lead's `wait-for --until gate` reads the run's end off the head (failed), said after the refusal
        told = (
            f"\n{failed(before[0], tree, command, code, machine)}"
            if code and not ASKED.get()
            else ""
        )
        if machine:
            raise Refused(
                f"{command} hit a machine fault (exit {code}, loop.md's `machine_exit`): {nothing}; fix the machine,"
                f" then run it again{told}"
            )
        if code:
            raise Refused(f"{command} exited {code}: {nothing}{told}")
        raise Refused(
            f"{command} changed the checkout, so it did not run on the head as pushed:\n{done['changed']}"
        )
    result = whole(done["records"], passed, tree)
    # ponytail: with a step left for a host the gate has no result here, so it ends no event: the `waiting` one goes
    # silent (wait exit 4) rather than saying `ok` for a gate that passed in part
    if done["left"]:
        ENDED["state"] = "left"  # told apart by detach() from a run that ended without writing one
    else:
        gate_event(branch, "ok", lambda: gate_url(result))
    return result | ({"left": done["left"]} if done["left"] else {})


def run_steps(
    commit: str,
    tree: str,
    changes: Callable[[], str],
    cwd: Path | None = None,
    out: Any = None,
    passed: list[str] | tuple[str, ...] = (),
    land: bool = False,
    beat: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Run each gate step (gate_plan, with `passed`) that has no pass on `tree` (step_pass) and may run here (here),
    in order, each under the host gate slot (ran_step), and record each that passes as run on `commit`, with the
    seconds it ran and the seconds it waited for the slot (stored). A step with a pass is reused and a `host` step in a
    cloud session left, each said on one line naming where the reused pass was recorded, so the log says which steps
    ran. It stops at the first step that fails or changes the checkout (`changes`: what changed, empty when nothing
    did), recording nothing for it. Returns `code` (that step's exit, else 0), `step` (that step, else None),
    `changed`, `records` (each passed step's record, in order), `ran` and `left` (step names)."""
    done: dict[str, Any] = {
        "code": 0,
        "step": None,
        "changed": "",
        "records": [],
        "ran": [],
        "left": [],
    }
    for step in gate_plan(passed, every=True):
        # a gate without `gate_steps` says nothing more than it did: its one step is the gate
        say = print if not step["legacy"] else (lambda *_a, **_k: None)
        if step["on_demand"] and not ASKED.get():
            say(skipped(step))  # neither run nor left: the gate's pass does not need it
            continue
        if (found := step_pass(commit, tree, step)) is not None:
            if found["tree"] == tree:
                say(f"{step['name']}: passed on tree {tree} before, reused from {origin_of(found)}")
            else:
                say(
                    f"{step['name']}: reused, inputs unchanged since {found['tree']} (from {origin_of(found)})"
                )
            done["records"].append(found)
            continue
        if not here(step):
            say(
                f"{step['name']}: no pass on tree {tree}, and it runs only where `where: {step['where']}` allows"
            )
            done["left"].append(step["name"])
            continue
        load: dict[str, Any] = {}
        code, seconds, waited = ran_step(
            step, cwd, out, load, land, **({"beat": beat} if beat else {})
        )
        done["ran"].append(step["name"])
        if code or (changed := changes()):
            return done | {"code": code, "step": step, "changed": "" if code else changed}
        record = (
            {
                "head": commit,
                "tree": tree,
                "command": step["command"],
                "exit": 0,
                "env": step["sets"],
                "time": now(),
                "host": socket.gethostname(),
                "seconds": seconds,
                "slot_wait_seconds": waited,
                **{key: load.get(key) for key in LOADED},
            }
            | step_field(step)
            | inputs_field(tree, step)
        )
        stored(record)
        say(
            f"{step['name']}: passed in {seconds} s, after {waited} s waiting for the host gate slot"
        )
        done["records"].append(record)
    return done


def head_moved(commit: str, tree: str, now: str, now_tree: str) -> str:
    """What a gate step did to where the run stands, empty when nothing: HEAD at another commit, or another tree, clean
    or not, means the step's pass would be recorded for `tree` though the steps after it run on another (run_steps)."""
    if (now, now_tree) == (commit, tree):
        return ""
    return f"HEAD moved during the gate: it is {now} (tree {now_tree}), not {commit} (tree {tree})"


def stored(record: dict[str, Any]) -> None:
    """Write a pass this clone ran to its record file (recorded), whole or not at all: two gates on one tree and command
    run at once on two slots (gate_slots), and a plain write truncates the file first, so a reader between the two
    would find it empty. The record goes to a file of this writer's own, then replaces the shared one in one step: named
    by process and thread, since two gates in one process (threads) sharing one file would have the second's replace
    find it gone."""
    where = recorded(record["tree"], record["command"])
    where.parent.mkdir(parents=True, exist_ok=True)
    partial = where.with_name(f"{where.name}.{os.getpid()}.{threading.get_ident()}.partial")
    try:
        partial.write_text(json.dumps(record, indent=2) + "\n")
        os.replace(partial, where)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise


def origin_of(record: dict[str, Any]) -> str:
    """Where a reused pass was recorded: its record branch on origin, else this clone's record of the run on a
    commit."""
    return record.get("ref") or f"this clone's record of its run on {record['head']}"


def whole(
    found: list[dict[str, Any]], passed: list[str] | tuple[str, ...] = (), tree: str | None = None
) -> dict[str, Any]:
    """The gate's pass made of its steps' passes `found`, in plan order (gate_plan): without `gate_steps`, the one
    record itself; else one built from them, as a pass is read (head, tree, command and env as gate() names them,
    exit, time, seconds and slot_wait_seconds), the newest step's commit and time, the seconds summed, and `steps`,
    each step's record. Its tree is `tree`, the one gated, when given, since a step reused by its inputs (#963)
    passed on another. Nothing writes it: each step's record is the stored and published pass."""
    plan = gate_plan(passed)
    if plan[0]["legacy"]:
        return found[0] if found else {}
    command, _, env = gate(passed)
    newest = max(found, key=lambda record: record["time"]) if found else {}
    return {
        "head": newest.get("head"),
        "tree": tree or newest.get("tree"),
        "command": command,
        "exit": 0,
        "env": env,
        "time": newest.get("time"),
        "seconds": sum(record.get("seconds", 0) for record in found),
        "slot_wait_seconds": sum(record.get("slot_wait_seconds", 0) for record in found),
        "steps": found,
        "published": all(record.get("published") for record in found),
    }


DETACHED = "APPROVALS_LOCAL_CI_HEAD"
RAN: dict[
    str, str
] = {}  # the gate command this process's local-ci ran, for the detached run to record (finished)
ENDED: dict[
    str, str
] = {}  # `state`: the end event this process wrote (`ok`, `failed`), or `left` for a host step's


def leads_its_session() -> bool:
    """Whether this process leads its own session, so no caller's process group or terminal takes it down."""
    return os.getsid(0) == os.getpid()


def run_files(head: str) -> tuple[Path, Path, Path]:
    """The detached local-ci run's log, pid and exit files on `head`, beside the records: a run is keyed by the commit
    it was started on, since its tree and env are known only once it runs."""
    where = local_ci_dir() / head
    return where.with_suffix(".log"), where.with_suffix(".pid"), where.with_suffix(".exit")


def since(pid: int) -> str | None:
    """When process `pid` started, as ps prints it in the C locale (so the write and every check read the same
    text), or None for no such process."""
    shown = subprocess.run(
        ["ps", "-o", "lstart=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "LC_ALL": "C"},
    )
    return shown.stdout.strip() or None


def lives(run: dict[str, Any]) -> bool:
    """Whether the pid file's run still lives: its pid and that process's start time, since the system hands a dead
    run's pid to the next process it starts."""
    now = since(run["pid"])
    return now is not None and now == run.get("since")


def going(started: str, exit_file: Path) -> dict[str, Any] | None:
    """The pid file's run, while it has written no exit code and its process lives; else None."""
    try:
        run = json.loads(started)
        return run if not exit_file.exists() and lives(run) else None
    except (ValueError, TypeError, KeyError):
        return None


def detach(argv: list[str]) -> int:
    """Run `local-ci` again in a session of its own, stdin from /dev/null and output to the log, and follow the log,
    exiting with its exit code, so a caller killed with its process group leaves the check to finish and record. A run
    already going on the head is followed instead; one started with other arguments is followed to its end, then this
    one starts."""
    head = git("rev-parse", "HEAD").strip()
    log, pid, exit_file = run_files(head)
    log.parent.mkdir(parents=True, exist_ok=True)
    # `waiting` before the run starts or any slow step, so a reader that waits once this returns never takes the previous
    # head's end for this one's; a pass reused on this head writes `ok` over it at once
    gate_event(gate_branch(), "waiting")
    while True:
        with open(pid, "a+") as claim:
            fcntl.flock(claim, fcntl.LOCK_EX)  # two callers at once start one run
            claim.seek(0)
            run, started = None, going(claim.read(), exit_file)
            if started is None:
                exit_file.unlink(missing_ok=True)
                ended_file(head).unlink(missing_ok=True)
                with open(log, "wb") as out, open(os.devnull) as nothing:
                    run = subprocess.Popen(
                        [sys.executable, os.path.abspath(__file__), *argv],
                        stdin=nothing,
                        stdout=out,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                        env={**os.environ, DETACHED: head},
                    )
                started = {"pid": run.pid, "since": since(run.pid), "argv": argv}
                claim.truncate(0)
                claim.write(json.dumps(started))
        code = follow(log, exit_file, started, run)
        if not ended_file(head).exists():
            # the run wrote no end event (a refusal before the gate, `--record` on a changed tree, a child killed or
            # crashed): end it here, so a `wait` reports the end and `waiting` does not go silent. A step left for a
            # host writes `left` and keeps its `waiting` on purpose (run_local_ci).
            gate_event(gate_branch(), "failed" if code else "ok", str(log) if code else "")
        if started["argv"] == argv:
            return code


def follow(
    log: Path, exit_file: Path, started: dict[str, Any], child: subprocess.Popen[bytes] | None
) -> int:
    """Print the log as it grows until the run ends; its exit code, or 1 for a run that ended without writing one."""
    with open(log, "rb") as lines:
        while True:
            # a run this call started is its child: poll reaps it, where a zombie would still show in ps
            running = child.poll() is None if child else lives(started)
            # checked before the read, so the last read holds everything
            ended = exit_file.exists() or not running
            sys.stdout.buffer.write(lines.read())
            sys.stdout.flush()
            if ended:
                break
            time.sleep(0.2)
    try:
        return int(exit_file.read_text())
    except (OSError, ValueError):
        print(f"the detached local-ci run ended without an exit code: its log is {log}")
        return 1


def ended_file(head: str) -> Path:
    """Where the detached run on `head` records the end event it wrote (ENDED), beside its exit code."""
    return run_files(head)[2].with_suffix(".event")


def ran_command_file(head: str) -> Path:
    """Where the detached run on `head` records the gate command it ran, beside its exit code."""
    return run_files(head)[2].with_suffix(".command")


def finished(head: str, code: int, command: str | None = None) -> None:
    """The detached run's last step: the gate command it ran (with a head proof, under the caller's tier), then its
    exit code, written whole, after all its output."""
    sys.stdout.flush()
    sys.stderr.flush()
    _, _, exit_file = run_files(head)
    if command:
        ran_command_file(head).write_text(command + "\n")
    if ENDED.get("state"):
        ended_file(head).write_text(ENDED["state"] + "\n")
    partial = exit_file.with_suffix(".exit-partial")
    partial.write_text(f"{code}\n")
    os.replace(partial, exit_file)


# the Mergify config the pack ships, which queue_config renders with the policy's `land` batching keys
MERGIFY_TEMPLATE = Path(__file__).resolve().parents[1] / "mergify.yml"


def duration(seconds: int) -> str:
    """`seconds` as a Mergify duration: whole hours or minutes where it divides, else seconds."""
    size, unit = next(
        (s, u) for s, u in ((3600, "hour"), (60, "minute"), (1, "second")) if seconds % s == 0
    )
    count = seconds // size
    return f"{count} {unit}{'' if count == 1 else 's'}"


def queue_config(policy: str | None = None) -> str:
    """`queue-config`: the Mergify config a repo under `land.queue: mergify` commits as `.mergify.yml`, the pack's
    template (mergify.yml) with gatekeeper.md's `land` batching: `batch_size` (1 is one PR per entry) as the queue
    rule's `batch_size`, `batch_wait_seconds` as its `batch_max_wait_time`; a key the policy leaves out keeps the
    template's value. The policy is the checkout's own (gatekeeper.md, else loop.md's `## Settings`), not the base's,
    since the file it renders is the checkout's too."""
    if policy is None:
        root = toplevel()
        files = [root / path for path in (LOOP, POLICY)]
        policy = chosen(*(read(path) if path.exists() else None for path in files))
    return mergify_rendered(policy)


def mergify_rendered(policy: str | None) -> str:
    """queue_config's render of the policy text `policy` (None: no policy, every default): the template with the
    `land` batch keys, and its queue's `checks_timeout` set to `timeouts.run` (#2228), 1 hour as the template's with
    nothing set, so with neither the file is the template byte for byte."""
    land = settings(policy).get("land") or {}
    text = MERGIFY_TEMPLATE.read_text()
    for key, value in (
        ("batch_size", land.get("batch_size")),
        (
            "batch_max_wait_time",
            None if "batch_wait_seconds" not in land else duration(land["batch_wait_seconds"]),
        ),
        ("checks_timeout", said_span(timeouts(policy)["run"])),
    ):
        if value is not None:
            text, found = re.subn(
                rf"^(\s+{key}:) .*$", rf"\g<1> {value}", text, count=1, flags=re.MULTILINE
            )
            if not found:
                raise Refused(f"{MERGIFY_TEMPLATE} has no `{key}` line to render")
    return text


def gate_record(head: str, words: list[str]) -> int:
    """`gate-record <head> [-- <command>]`: whether origin holds a pass record (record_ref) of the gate, each step of
    `gate_steps` (gate_plan), or of `<command>`, on `head`'s tree, so the verifier reads it instead of running the
    same tests again. Exit 0 names each record branch; exit 1 names each command with
    no record and why, which the verifier then runs. A record is keyed by the tree and the command's digest
    (record_name), so another tree, another command or another test list in it is another record and never reused;
    a step whose `paths` key it by its inputs (inputs_key) is found by that key on any tree instead, naming the tree
    and key it was found by (origin_input_pass). Only origin counts, the branch `local-ci --record` pushes: never this clone's own record file, which a worker
    sharing the clone may have written, and never a worker's log or report."""
    # tree_of fetches a head this clone lacks from origin, as reusable() does
    if (held := tree_of(head)) is None:
        print(f"no tree for {head}: run the tests")
        return 1
    # a command the gate's steps (gate_plan) name is read as that step, so one keyed by its inputs (inputs_key) is
    # found by that key on any tree (origin_input_pass); any other command is keyed by the tree alone
    # (every step, so an `on_demand` one's pass is found by its key when its command is asked; the gate alone is the
    # steps it needs)
    steps = {step["command"]: step for step in gate_plan(every=True)}
    # a narrowed command (narrowed_gate) is the words of its run, then NARROWED, which shlex.join would quote
    if words[-1:] == [NARROWED.strip()]:
        commands = [shlex.join(words[:-1]) + NARROWED]
    else:
        commands = [shlex.join(words)] if words else [step["command"] for step in gate_plan()]
    missing = 0
    for command in commands:
        why: list[str] = []
        step = steps.get(command)
        if step is not None and (key := inputs_key(held, step)) is not None:
            record = origin_input_pass(key, step, why)
            if record is None:
                missing += 1
                reason = (
                    "; ".join(why)
                    or f"origin holds no record branch of it with the inputs key {key}"
                )
                print(
                    f"no record of `{command}` with inputs {key} on tree {held}: run it ({reason})"
                )
            else:
                print(
                    f"recorded by inputs: {described(record['tree'], record['head'], command)}, as {record['ref']}"
                    f" at {record['time']}, reused on tree {held}, inputs key {key}"
                )
            continue
        written = command[: -len(NARROWED)] if narrowed_base(command) is not None else command
        record = record_ref(held, command, command_line(written, "gate")[2], why)
        if record is None:
            missing += 1
            reason = (
                "; ".join(why)
                or f"origin holds no record branch {RECORD_BRANCH.format(record_name(held, command))}"
            )
            print(f"no record of `{command}` on tree {held}: run it ({reason})")
        else:
            print(
                f"recorded: {described(held, record['head'], command)}, as {record['ref']} at {record['time']}"
            )
    return 1 if missing else 0


def local_ci_status(head: str) -> int:
    """Say whether the detached local-ci run on `head` is running (exit 3), passed (0) or failed (1), with its log;
    passed only when a pass on `head`'s tree under this environment is recorded: with `gate_steps`, of each step the
    run had to pass (ran_command_file), here or, for a step it reused, on origin (record_ref)."""
    head = git("rev-parse", "--verify", f"{head}^{{commit}}").strip()
    log, pid, exit_file = run_files(head)
    try:
        code = int(exit_file.read_text())
    except (OSError, ValueError):
        code = None
    plain = [step["command"] for step in gate_plan() if here(step)]
    try:
        # what the run ran, one command per line, else the plain gate
        commands = ran_command_file(head).read_text().strip().splitlines() or plain
    except OSError:
        commands = plain
    command, held = "; ".join(commands), str(tree_of(head))
    single = len(commands) == 1
    # a step keyed by its inputs (inputs_key) may have passed on another tree with the same inputs
    keyed = {
        step["command"]: step
        for step in gate_plan(every=True)
        if inputs_key(held, step) is not None
    }
    if code == 0 and not all(
        recorded(held, one).exists()
        or not single
        and record_ref(held, one, command_line(one, "gate")[2]) is not None
        or one in keyed
        and step_pass(head, held, keyed[one], show=False) is not None
        for one in commands
    ):
        # a run on a checkout with changes, or on another PR's head, exits 0 with no pass on this one
        print(f"exited 0 without recording a pass on {head} under the gate's command: log {log}")
        return 1
    if code is not None:
        under = f" under `{command}`" if commands != plain else ""
        if bases := sorted({base for one in commands if (base := narrowed_base(one))}):
            under += f", narrowed against {', '.join(bases)}"
        print(f"{'passed' if code == 0 else f'failed (exit {code})'} on {head}{under}: log {log}")
        return 0 if code == 0 else 1
    started = going(pid.read_text() if pid.exists() else "", exit_file)
    if started:
        print(f"running on {head} (pid {started['pid']}): log {log}")
        return 3
    if log.exists():
        print(f"ended without an exit code on {head}: log {log}")
    else:
        print(f"no detached local-ci run on {head}")
    return 1


def local_ci_env() -> dict[str, str | None]:
    """The value of each variable the policy's `local_ci_env` names, None for one unset; {} without the setting."""
    policy = project()[1]
    names = settings(policy).get("local_ci_env", [])
    if bad := [name for name in names if not re.fullmatch(r"[A-Za-z_]\w*", name)]:
        raise Refused(
            f"{source(policy)}'s setting `local_ci_env` names {bad[0]!r}, which is no variable name"
        )
    return {name: os.environ.get(name) for name in sorted(names)}


def env_line(env: dict[str, str | None]) -> str:
    """The comment's line naming the environment the check ran under; unset names are left out."""
    named = [f"{name}={value}" for name, value in env.items() if value is not None]
    return f"Env: {' '.join(named) or 'default'}\n"


def reusable(
    head: str, passed: list[str] | tuple[str, ...] = (), why: list[str] | None = None
) -> dict[str, Any] | None:
    """A green pass on `head`'s tree under today's gate (with `passed`, gate), or None: a pass of each of its steps
    (gate_plan, step_pass), put together (whole). The record's `head` is the commit the pass ran on. With `why`, the
    reason origin's record branch, found, was rejected is appended to it (record_ref), and with `gate_steps` each step
    with no pass is named."""
    if (held := tree_of(head)) is None:
        return None
    found = []
    for step in gate_plan(passed):
        if (record := step_pass(head, held, step, why)) is None:
            if why is not None and not step["legacy"]:
                why.append(
                    f"the gate step `{step['name']}` ({step['command']}) has no pass on tree {held}"
                )
            return None
        found.append(record)
    return whole(found, passed, held)


def step_pass(
    head: str, held: str, step: dict[str, Any], why: list[str] | None = None, show: bool = True
) -> dict[str, Any] | None:
    """A green pass of gate step `step` (gate_plan) on tree `held` (`head`'s), or None: this clone's record, made on
    any commit with that tree, else one another clone pushed to origin for that tree (published). A record without
    `env` (0.23.1 wrote none) never matches, since nothing says what tier it ran; nor does a failed one, which is never
    recorded. With `why`, the reason origin's record branch, found, was rejected is appended to it (record_ref).
    Without `show`, origin's record is read without writing the `gate` status on `head` (published). A step whose
    `paths` give it an inputs key on `held` (inputs_key) is found by that key alone (input_pass), on any tree. A step
    `select.unkeyed` lists has none: it reads what no key covers (a live service, a rotating credential, the host), so
    the same tree can give another result, and every run decision (run_steps, reusable) runs it again. Nor does any
    step under FRESH (`local-ci --record --rerun`), which runs every step afresh."""
    if step.get("unkeyed") or FRESH.get():
        return None
    if (key := inputs_key(held, step)) is not None:
        return input_pass(head, key, step, why, show)
    command, env = step["command"], step["sets"]
    try:
        record = json.loads(recorded(held, command).read_text())
    except (OSError, ValueError):
        record = None
    # the posting path reads the commit and time too: a record without them is a miss, not a crash
    if (
        isinstance(record, dict)
        and all(isinstance(record.get(key), str) for key in ("head", "time"))
        and (record.get("tree"), record.get("command"), record.get("exit"), record.get("env"))
        == (held, command, 0, env)
    ):
        return record | step_field(step)
    found = (
        published(head, held, command, env, why) if show else record_ref(held, command, env, why)
    )
    return found | step_field(step) if found is not None else None


def input_pass(
    head: str,
    key: str,
    step: dict[str, Any],
    why: list[str] | None = None,
    show: bool = True,
) -> dict[str, Any] | None:
    """A green pass of gate step `step` on any tree whose inputs key (inputs_key) is `key`, or None: this
    clone's record of the step's command holding `key`, else origin's record branch for a tree whose `Inputs:` line
    says it (input_candidates), sound as record_ref reads one. Either counts only when today's `paths` give its own
    tree the same key, so a record saying a key its tree doesn't hold counts for nothing; of this clone's, the newest.
    With `why` and `show` as step_pass's."""
    command, env, name = step["command"], step["sets"], step["name"]
    found = []
    with contextlib.suppress(OSError):
        for path in local_ci_dir().glob(f"*-{gate_key(command)}.json"):
            try:
                record = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if (
                isinstance(record, dict)
                and all(isinstance(record.get(field), str) for field in ("head", "time", "tree"))
                and (
                    record.get("command"),
                    record.get("exit"),
                    record.get("env"),
                    record.get("step"),
                    record.get("inputs"),
                )
                == (command, 0, env, name, key)
                and inputs_key(record["tree"], step) == key
            ):
                found.append(record)
    if found:
        return max(found, key=lambda record: record["time"])
    record = origin_input_pass(key, step, why)
    if record is not None and show:
        shown(head, record)
    return record


def origin_input_pass(
    key: str, step: dict[str, Any], why: list[str] | None = None
) -> dict[str, Any] | None:
    """input_pass's read of origin alone: the record branch of gate step `step`'s command, on a tree whose `Inputs:`
    line says `key` (input_candidates) and whose own tree today's `paths` give that key, sound as record_ref reads
    one, or None. With `why` as record_ref's."""
    command, env = step["command"], step["sets"]
    for tree in input_candidates(command, key, why):
        record = record_ref(tree, command, env, why)
        if record is not None and inputs_key(tree, step) == key:
            return record | step_field(step)
    return None


def input_candidates(command: str, key: str, why: list[str] | None = None) -> list[str]:
    """The trees of origin's record branches of `command` whose message says the inputs key `key` (record_message),
    each still to be read as record_ref reads one; origin unreachable: none, with the reason appended to `why`."""
    pattern = RECORD_BRANCH.format(f"*-{gate_key(command)}")
    trees = []
    try:
        listed = [line.split("\t") for line in git("ls-remote", "origin", pattern).splitlines()]
        if not listed:
            return []
        names = {ref: ref.rsplit("/", 1)[1] for _, ref in listed}
        git(
            "fetch",
            "-q",
            "--no-tags",
            "origin",
            *(f"+{ref}:refs/loop/records/{name}" for ref, name in names.items()),
        )
        for commit, ref in listed:
            message = git("cat-file", "commit", commit).partition("\n\n")[2]
            if f"{INPUTS}{key}" in message.splitlines()[1:]:
                trees.append(names[ref].split("-", 1)[0])
    except (subprocess.CalledProcessError, OSError, ValueError) as exc:
        if why is not None:
            cause = " ".join((getattr(exc, "stderr", None) or str(exc)).split())
            why.append(f"origin's record branches for `{command}` could not be read: {cause}")
        return []
    return trees


def inputs_field(tree: str, step: dict[str, Any]) -> dict[str, str]:
    """A gate step's record's `inputs` field, its inputs key on `tree` (inputs_key); none for a step keyed by tree."""
    key = inputs_key(tree, step)
    return {"inputs": key} if key else {}


def step_field(step: dict[str, Any]) -> dict[str, str]:
    """A gate step's record's `step` field, its name; none for the one step of a gate without `gate_steps`."""
    return {} if step["legacy"] else {"step": step["name"]}


def published(
    head: str,
    held: str,
    command: str,
    env: dict[str, str] | None = None,
    why: list[str] | None = None,
) -> dict[str, Any] | None:
    """A pass of `command` (setting `env`, default the gate's) on `head`'s tree `held` that a clone pushed to origin
    as its record branch (publish, record_ref), as a record, or None. It is the one route another clone's pass is read
    by: a `gate` status is only shown, never read, since anyone who can write one may type it. A reader that finds the
    record writes the `gate` status on `head`, so the PR shows the pass, where the token allows."""
    record = record_ref(held, command, env if env is not None else gate()[2], why)
    if record is not None:
        shown(head, record)
    return record


def record_name(tree: str, command: str) -> str:
    """A pushed record's name: the tree it passed on and the gate command's digest (gate_key)."""
    return f"{tree}-{gate_key(command)}"


# a pushed record's lines for the seconds its run took and waited for the host gate slot (record_message)
TIMED = {"seconds": "Seconds: ", "slot_wait_seconds": "Slot wait seconds: "}
# a record's fields and a pushed record's lines for the host's load at the step's start (host_load), and the line for
# its inputs key (inputs_key)
LOADED = {"load_1m": "Load 1m: ", "swap_used_mb": "Swap used MB: "}
INPUTS = "Inputs: "


def record_message(record: dict[str, Any]) -> str:
    """A pushed record's commit message: the status description (described), then the env the gate ran under, and
    the seconds it ran and waited for the slot where the record holds them (TIMED)."""
    env = record.get("env", gate()[2])
    timed = "".join(
        f"{line}{record[key]}\n" for key, line in TIMED.items() if isinstance(record.get(key), int)
    )
    loaded = "".join(
        f"{line}{record[key]}\n"
        for key, line in LOADED.items()
        if type(record.get(key)) in (int, float)
    )
    inputs = f"{INPUTS}{record['inputs']}\n" if record.get("inputs") else ""
    return (
        f"{described(record['tree'], record['head'], record['command'])}\n\n"
        f"Env: {json.dumps(env, sort_keys=True)}\n{timed}{loaded}{inputs}"
    )


def record_ref(
    held: str, command: str, env: dict[str, str], why: list[str] | None = None
) -> dict[str, Any] | None:
    """A pass of `command` under `env` on tree `held` pushed to origin as its record branch (push_record), as a
    record, or None. Only origin is read, the repo `land` pushes its lock to, so a branch on a PR's fork never counts:
    one in the base repo is its write access. The branch's commit must hold `held` as its tree, its parent the commit
    its description names, and the env line `env`, so a record whose content is not what its name says counts for
    nothing. origin unreachable: None, so `local-ci <pr>` refuses unless it is given `--run`. With `why`, a line
    naming the branch found and why it was rejected (or that origin could not be read) is appended to it, so the
    refusal says what to push."""
    name = record_name(held, command)
    ref = RECORD_BRANCH.format(name)
    try:
        listed = git("ls-remote", "origin", ref).split()
        if ref not in listed[1::2]:
            return None
        git("fetch", "-q", "--no-tags", "origin", f"+{ref}:refs/loop/records/{name}")
        body = git("cat-file", "commit", listed[listed.index(ref) - 1])
    except (subprocess.CalledProcessError, OSError) as exc:
        if why is not None:
            cause = " ".join((getattr(exc, "stderr", None) or str(exc)).split())
            why.append(f"origin's record branch {ref} could not be read: {cause}")
        return None
    header, _, message = body.partition("\n\n")
    fields = [line.split(" ", 1) for line in header.splitlines()]
    trees = [value for key, value in fields if key == "tree"]
    parents = [value for key, value in fields if key == "parent"]
    committed = [value for key, value in fields if key == "committer"]
    lines = message.splitlines()
    env_wanted = f"Env: {json.dumps(env, sort_keys=True)}"
    if trees != [held]:
        rejected = f"its commit holds the tree {' '.join(trees) or 'none'}, not {held}"
    elif len(parents) != 1:
        rejected = f"its commit has {len(parents)} parents, not the one commit the pass ran on"
    elif not lines or lines[0] != described(held, parents[0], command):
        found = repr(lines[0]) if lines else "nothing"
        rejected = f"its description reads {found}, not {described(held, parents[0], command)!r}"
    elif env_wanted not in lines[1:]:
        rejected = f"its message lacks the line {env_wanted!r}, the env the gate runs under now"
    elif not committed:
        rejected = "its commit names no committer, so its time is unknown"
    else:
        rejected = ""
    if rejected:
        if why is not None:
            why.append(f"origin holds the record branch {ref}, but it is not this pass: {rejected}")
        return None
    when = int(committed[0].rsplit(" ", 2)[1])
    # a record pushed before #940 names no seconds: still a pass, with its seconds unknown
    timed = {
        key: int(line[len(prefix) :])
        for key, prefix in TIMED.items()
        for line in lines[1:]
        if line.startswith(prefix) and line[len(prefix) :].isdigit()
    }
    loaded: dict[str, Any] = {}
    for key, prefix in LOADED.items():
        for line in lines[1:]:
            if line.startswith(prefix):
                with contextlib.suppress(ValueError):
                    value = float(line[len(prefix) :])
                    loaded[key] = int(value) if key == "swap_used_mb" else value
    inputs = [line[len(INPUTS) :] for line in lines[1:] if line.startswith(INPUTS)]
    return {
        "head": parents[0],
        "tree": held,
        "command": command,
        "exit": 0,
        "env": env,
        "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(when)),
        **timed,
        **loaded,
        **({"inputs": inputs[0]} if inputs else {}),
        "published": True,
        "ref": ref,
    }


def push_record(record: dict[str, Any]) -> str:
    """Push a pass to origin as its record branch (RECORD_BRANCH) on a commit holding its tree, its parent the commit
    it ran on, its message the status description and env (record_message), and return the ref. The commit's author,
    committer and dates are fixed, so pushing a pass again pushes the same commit. Refused: CalledProcessError."""
    ref = RECORD_BRANCH.format(record_name(record["tree"], record["command"]))
    git("push", "-q", "--no-verify", "origin", f"{record_commit(record)}:{ref}")
    return ref


def record_commit(record: dict[str, Any]) -> str:
    """The commit push_record pushes for a pass: its tree, its parent the commit the pass ran on, its message
    record_message, with fixed author, committer and dates, so the same pass always makes the same commit."""
    when = {"GIT_COMMITTER_DATE": record["time"], "GIT_AUTHOR_DATE": record["time"]}
    return subprocess.run(
        [
            "git",
            "commit-tree",
            record["tree"],
            "-p",
            record["head"],
            "-m",
            record_message(record),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=os.environ | RECORDER | (when if record.get("time") else {}),
    ).stdout.strip()


def drop_records(heads: list[str]) -> None:
    """Delete from origin the record branches no head needs any more: each of the trees of `heads`, landed now, and
    each whose tree is no open PR's head tree and whose record is over a day old (RECORD_AGE), a head that was
    replaced or never landed. The day spares a record pushed before its PR was opened. Only a host deletes, since a
    cloud session can't delete a ref; a refusal is said, and the branch stays for the next land. The open PRs
    unreadable, or an open PR's head tree unknown here (a fork's head this clone can't fetch): only the landed trees'
    branches go, since the unknown tree may be one an old record holds."""
    landed = {held for head in heads if (held := tree_of(head))}
    try:
        open_trees: set[str | None] | None = {tree_of(head) for head in open_heads()}
    except (subprocess.CalledProcessError, OSError, ValueError):
        open_trees = None
    if open_trees is not None and None in open_trees:
        open_trees = None
    try:
        git(
            "fetch",
            "-q",
            "--prune",
            "--no-tags",
            "origin",
            f"+{RECORD_BRANCH.format('*')}:refs/loop/records/*",
        )
        listed = git(
            "for-each-ref",
            "--format=%(refname:lstrip=3) %(committerdate:unix)",
            "refs/loop/records/",
        )
    except (subprocess.CalledProcessError, OSError):
        return
    old = time.time() - RECORD_AGE
    for name, when in (line.split() for line in listed.splitlines() if len(line.split()) == 2):
        held = name.split("-", 1)[0]
        stale = open_trees is not None and held not in open_trees and int(when) < old
        if held not in landed and not stale:
            continue
        try:
            git("push", "-q", "--no-verify", "origin", f":{RECORD_BRANCH.format(name)}")
        except subprocess.CalledProcessError as exc:
            print(
                f"the record branch {RECORD_BRANCH.format(name)} was not deleted: {' '.join((exc.stderr or '').split())}"
            )


def stated(
    head: str, context: str, wanted: Callable[[dict[str, Any], re.Match[str]], bool]
) -> tuple[dict[str, Any], re.Match[str]] | None:
    """The newest green status `context` on `head` whose description reads as a pass (PASSED) and is `wanted`, with
    that reading, or None. It counts only from the account running this or a writer, since a status is the repo's to
    write and a PR's author may hold no more than a fork. GitHub unreachable: None, so the check runs."""
    try:
        found = statuses(head)
    except (subprocess.CalledProcessError, OSError, ValueError):
        return None
    for status in found:  # newest first
        named = PASSED.fullmatch(status.get("description") or "")
        login = (status.get("creator") or {}).get("login")
        if (status.get("context"), status.get("state")) != (context, "success") or not named:
            continue
        if wanted(status, named) and login is not None and (login == viewer() or writer(login)):
            return status, named
    return None


def head_proof(loop: str | None) -> dict[str, Any] | None:
    """loop.md's `head_proof` (`status`, the commit status `land` requires on each head, and `env`, the names
    `local-ci --record --head-proof` takes from the caller), or None without it."""
    proof: dict[str, Any] | None = settings(loop).get("head_proof")
    if proof and (
        bad := [name for name in proof["env"] if not re.fullmatch(r"[A-Za-z_]\w*", name)]
    ):
        raise Refused(f"loop.md's `head_proof.env` names {bad[0]!r}, which is no variable name")
    # a context this script or the approvals workflow posts would let a plain gate pass or a go prove the head
    if proof and proof["status"] in {PUBLISHED, OWNER_GO, *OWN}:
        raise Refused(
            f"loop.md's `head_proof.status` is {proof['status']!r}, a status the loop already posts for another proof"
        )
    return proof


def proof_line(record: dict[str, Any], names: list[str]) -> str:
    """A head proof's status description: the tree, the commit the pass ran on, and the value of each of `names` it
    ran under (`default` for none), so the verifier reads the values off the head."""
    values = " ".join(
        f"{name}={public(record['env'][name])}" for name in names if name in record["env"]
    )
    # ponytail: cut at GitHub's limit, which values past 77 characters reach; a digest if a tier ever gets that long
    return f"Tree {record['tree']} on {record['head'][:12]}: {values or 'default'}"[:DESCRIBED]


def proved(head: str, status: str) -> bool:
    """Whether `head` itself carries a green `status`, the head proof, naming its own tree (stated): a status on an
    older head never counts, since a status belongs to one commit."""
    held = tree_of(head)
    return (
        held is not None
        and stated(head, status, lambda _status, named: named[1] == held) is not None
    )


def shown(head: str, record: dict[str, Any]) -> str | None:
    """Write the `gate` status for `record` on `head`, which the PR page shows and nothing reads; refused or
    unreachable: why, which is no error, since the record branch is the pass."""
    try:
        post_status(head, PUBLISHED, described(record["tree"], record["head"], record["command"]))
    except (subprocess.CalledProcessError, OSError) as exc:
        return (getattr(exc, "stderr", None) or str(exc)).strip()
    return None


def failed(head: str, tree: str, command: str, code: int, machine: bool) -> str:
    """Write a failed run's `gate` status on `head`, `failure` (`error` for a machine fault, which says nothing of the
    change), so a lead waiting on the head (the omnigent-agents skill's `wait-for --until gate`) learns the run ended without a
    pass; returns the line saying so, or, refused or unreachable, why, which is no error, as a pass's is (shown)."""
    line = f"Failed: {command} exited {code} on tree {tree[:12]} at {head[:12]}"[:DESCRIBED]
    try:
        post_status(head, PUBLISHED, line, "error" if machine else "failure")
    except (subprocess.CalledProcessError, OSError) as exc:
        why = (getattr(exc, "stderr", None) or str(exc)).strip()
        return f"the `gate` failure status, which tells a waiting lead the run failed, was not written: {why}"
    return f"wrote the `gate` {'error' if machine else 'failure'} status on {head}"


def publish(
    head: str, record: dict[str, Any], proof: dict[str, Any] | None = None, show: bool = True
) -> None:
    """Publish a pass this clone holds. Without `proof`: push it to origin as its record branch (push_record), the one
    route by which another clone reuses it (published), then show it as the `gate` status on `head` (shown); each
    refusal is said, and a refused push leaves the pass in this clone only. With `proof`, loop.md's `head_proof`: post
    it as the commit status of the proof's `status`, the description naming the values of its `env` (proof_line),
    which `land` reads (proved), unless one already does. Without `show` (a run of named steps, `--step`), the record
    branches are pushed and no `gate` status written."""
    if proof is not None:
        context, line = proof["status"], proof_line(record, proof["env"])
        if stated(head, context, lambda status, _named: status["description"] == line):
            return
        try:
            post_status(head, context, line)
        except (subprocess.CalledProcessError, OSError) as exc:
            why = (getattr(exc, "stderr", None) or str(exc)).strip()
            print(f"not published on GitHub, so only this clone reuses it: {why}")
            return
        print(f"published on {head} as the commit status `{context}`")
        return
    # with `gate_steps`, each step's record is pushed by itself; one already in origin, read back from it
    # (record_ref), is left as it is, since pushing it again only fails and would say origin lacks it
    for one in record.get("steps", [record]):
        if KEEP.get():
            ref = RECORD_BRANCH.format(record_name(one["tree"], one["command"]))
            commit = record_commit(one | {"time": one.get("time") or now()})
            git("update-ref", ref, commit)
            print(f"{KEPT}{ref} {commit}")
            continue
        if one.get("published"):
            name = one.get("ref") or RECORD_BRANCH.format(record_name(one["tree"], one["command"]))
            print(f"already in origin as the record branch {name}, so it is not pushed again")
            continue
        try:
            ref = push_record(one | {"time": one.get("time") or now()})
        except (subprocess.CalledProcessError, OSError) as exc:
            why = (getattr(exc, "stderr", None) or str(exc)).strip()
            if FRESH.get():  # a rerun's record counts only on origin (rerun_gap): a branch already there is someone else's
                raise Refused(
                    f"the rerun's record was not pushed to origin, so no gate reads it: {why}"
                ) from None
            print(f"not pushed to origin, so only this clone reuses it: {why}")
        else:
            print(f"published in origin as the record branch {ref}")
    # a job that can't write keeps the record, and a run of named steps is no pass of the gate: neither writes a status
    if KEEP.get() or not show:
        return
    if why := shown(head, record):
        print(f"the `gate` status, which only shows the pass on the PR, was not written: {why}")


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def reused(record: dict[str, Any]) -> str:
    """How a reused pass is said: the commit it ran on, when, and its record branch; with `gate_steps`, each step's."""
    if "steps" in record:
        each = "; ".join(
            f"`{one['step']}` from the pass recorded on {one['head']} at {one['time']}"
            + (f" and pushed as {one['ref']}" if one.get("ref") else "")
            for one in record["steps"]
        )
        return f"Reused by tree, step by step: {each}; none run again."
    pushed = f" and pushed as {record['ref']}" if record.get("ref") else ""
    return f"Reused by tree from the pass recorded on {record['head']} at {record['time']}{pushed}, not run again."


def no_pass(head: str, why: list[str] | tuple[str, ...] = ()) -> str:
    """Why `local-ci <pr>` found no usable pass for `head`: the head, its tree, the record branch looked for, what
    was wrong with this clone's record (a missing field, another command or env), and `why`, the reasons reusable()
    gave for rejecting origin's record branch, one line each. With `gate_steps`, each line names its step, and a step
    whose record here is sound gets none."""
    held = tree_of(head)
    if held is None:
        return f"no pass for {head}: its tree is unknown here (fetch the head)"
    lines = []
    for step in gate_plan():
        command, env = step["command"], step["sets"]
        said = "" if step["legacy"] else f"step `{step['name']}`: "
        if step["unkeyed"]:
            lines.append(f"{said}`select.unkeyed` lists it, so its pass is never reused: run it")
            continue
        if (key := inputs_key(held, step)) is not None:
            if step_pass(head, held, step, show=False) is None:
                lines.append(
                    f"{said}no pass on its inputs in tree {held} (key {key[:12]}, the files under its `paths`):"
                    f" looked in this clone's records and origin's record branches of {command}"
                )
            continue
        ref = RECORD_BRANCH.format(record_name(held, command))
        found = []
        try:
            record = json.loads(recorded(held, command).read_text())
        except OSError:
            found.append("this clone holds no record for that tree and gate command")
        except ValueError:
            found.append("this clone's record is not JSON")
        else:
            if not isinstance(record, dict):
                found.append("this clone's record is not an object")
            else:
                for key in ("head", "time"):
                    if not isinstance(record.get(key), str):
                        found.append(f"this clone's record lacks its `{key}` field")
                for key, want in (("tree", held), ("command", command), ("exit", 0), ("env", env)):
                    if record.get(key) != want:
                        found.append(
                            f"this clone's record has {key} {record.get(key)!r}, not {want!r}"
                        )
        if said and not found:
            continue
        lines.append(
            f"{said}no pass for the head {head}, tree {held}: looked for the record branch {ref} on origin"
        )
        lines.extend(f"{said}{line}" for line in found)
    lines.extend(why)
    lines.append(
        "push the record (`local-ci --record` on the head) or run the gate here with `local-ci <pr> --run`"
    )
    return "\n".join(lines)


# `local-ci <pr> --head`: how many times the PR's head is read, and the seconds between reads (about 30 s in all)
HEAD_READS, HEAD_WAIT = 4, 10


def settled_head(pr: int, want: str | None) -> str:
    """The PR's head from GitHub. With `want` (the commit just pushed, in full), GitHub may still answer the previous
    head for a moment after a push, so a different head is read again, HEAD_READS times in all, HEAD_WAIT seconds apart;
    one still different is refused, naming both, before anything is read or posted on it. `want` is compared whole,
    never as a prefix, so a short or non-hex one is refused."""
    if want is not None and not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", want):
        raise Refused(
            f"--head {want}: the whole commit hash (40 hex digits), not a prefix or a ref"
        )
    for read in range(HEAD_READS):
        head = pull_status(pr)["headRefOid"]
        if want is None or head.lower() == want.lower():
            return head
        if read < HEAD_READS - 1:
            time.sleep(HEAD_WAIT)
            github.read_afresh()  # the next read is GitHub's, not this run's memory (#2016)
    raise Refused(
        f"PR #{pr}'s head on GitHub is {head}, not {want} after {HEAD_READS} reads over"
        f" {(HEAD_READS - 1) * HEAD_WAIT} s: nothing posted; run it again once GitHub shows the push"
    )


def local_ci(
    pr: int | None,
    proof: dict[str, Any] | None = None,
    run: bool = False,
    want: str | None = None,
    given: str | None = None,
    rerun: bool = False,
) -> int:
    """With a PR: post a pass on its head, from a recorded pass on its tree; with `run`, from a run now when there is
    none, and without it refuse then (no_pass). Without a PR: on a
    clean checkout, reuse or run and record a pass on HEAD, for `local-ci <pr>` to reuse; on one with changes, run the
    check and record nothing. Either publishes a pass this clone holds (publish). With `proof`, loop.md's
    `head_proof` (`--head-proof`, no PR), the gate takes its `env` names from this process (gate), and the pass is
    published as the head proof instead. With `gate_steps`, a step with a pass on the tree is reused and only the
    others run; in a cloud session a `host` step is left for a host, said, and the steps that passed are published,
    but no head proof, nor a pass for a PR, since the gate as a whole has none. With a PR the gate is the policy of the
    base it merges onto (policy_from); without one, the default branch's. With a PR it prints one line naming the
    head it posted on; `want` (`--head <sha>`) is the head the caller just pushed (settled_head). Without a PR or
    `proof`, in narrowed mode (narrowing) under `ci: false` or with `given` (`--base <rev>`), the gate is the narrowed
    one against `given`, else the merge base (selection_base, narrowed_gate). With `rerun` (`--record --rerun`, an
    independent rerun of the head's selection, which levels' `rerun` asks and rerun_gap reads), narrowed in narrowed
    mode whatever `ci` says, no pass is reused (FRESH), here or on origin, and the command runs with RERUN_VAR set in
    front of it, so its record is keyed apart from the worker's and says in its `Env:` line that it is a rerun."""
    if rerun:
        os.environ[RERUN_VAR] = "1"
        FRESH.set(True)
    if (
        pr is None
        and proof is None
        and NARROW_BASE.get() is None
        and narrowing() is not None
        and (given is not None or rerun or no_ci(project()[1]))
    ):
        if (base := selection_base(given)) is not None:
            with narrowed_to(base):
                return local_ci(None, None, run, want, rerun=rerun)
        print("local-ci: no base to narrow against: running the full check", file=sys.stderr)
    if pr is None:
        passed = proof["env"] if proof else (RERUN_VAR,) if rerun else ()
        # the commands this run must leave a pass of, one per line, for `--status` (ran_command_file)
        RAN["command"] = "\n".join(step["command"] for step in gate_plan(passed) if here(step))
        head, changes = checkout_state()
        if changes:
            deepened(None)
            command, code = gate(passed)[0], ran_gate(passed=passed)
            print(
                f"{command} exited {code} on a checkout with changes, so nothing is recorded:\n{changes}"
            )
            return code
        # a run of named steps (`--step`) says which, never that the gate passed
        said = f"Step {', '.join(ASKED.get())} passed" if ASKED.get() else CHECKED
        # a run of named steps goes through run_steps, which says by each step whether it reused a pass and from where
        if not ASKED.get() and (record := reusable(head, passed)):
            print(f"{said} on {head}: {reused(record)}")
            gate_event(gate_branch(), "ok", lambda: gate_url(record))
        else:
            record = run_local_ci(None, None, passed)
            where = (
                local_ci_dir() if "steps" in record else recorded(record["tree"], record["command"])
            )
            print(f"{said} on {head}: recorded in {where}")
        if ASKED.get():
            # each step's record pushed, with no `gate` status, which shows the gate's pass and this is none
            publish(
                head,
                {"steps": [one for one in record["steps"] if not one.get("published")]},
                show=False,
            )
            if left := record.get("left"):
                print(f"{head} has no pass of {runs_only(left)}, where `local-ci` runs it alone")
            return 0
        if left := record.get("left"):
            print(
                f"{head} has no pass on the whole gate yet: {runs_only(left)}, where `local-ci` runs it alone"
            )
            # each step by itself, so the `gate` status shows a step's pass, never the whole gate's
            for one in record["steps"]:
                if not one.get("published"):
                    publish(head, one)
            return 0
        if proof:
            publish(head, record, proof)
        elif not record.get("published"):
            publish(head, record)
        return 0
    with policy_from(base_branch(pr)):
        return pr_local_ci(pr, run, want)


def pr_local_ci(pr: int, run: bool, want: str | None = None) -> int:
    """local_ci for PR `pr`, under its base's policy."""
    head = settled_head(pr, want)
    why: list[str] = []
    loop = project()[1]
    # the PR is read only for a policy with an `incubating` section: no other project pays the read
    passed = tier(pull_request(pr), loop) if INCUBATING in settings(loop) else []
    # narrowed mode's gate of record on a `ci: false` head; a full pass on it is taken first
    if (
        NARROW_BASE.get() is None
        and no_ci(loop)
        and not reusable(head, passed)
        and (base := merge_base_of(pull_status(pr)["baseRefName"], head)) is not None
    ):
        with narrowed_to(base, head):
            if narrowed_gate(passed) is not None:
                return pr_local_ci(pr, run, want)
    if record := reusable(head, passed, why=why):
        source = f"{reused(record)}\n"
        gate_event(gate_branch(), "ok", lambda: gate_url(record))
    elif run:
        record = run_local_ci(pr, head, passed)
        source = ""
        if left := record.get("left"):
            raise Refused(
                f"no pass on the whole gate for {head}: {runs_only(left)}; the steps that passed here are recorded"
            )
    else:
        raise Refused(no_pass(head, why))
    if not record.get("published"):
        publish(head, record)
    comment(
        pr,
        fold(
            CHECKED,
            f"The gate passed on {head[:7]}.",
            f"Head: {head}\nCommand: {record['command']}\n{env_line(record['env'])}{source}",
        ),
    )
    print(f"{CHECKED} on {head}: posted on PR #{pr}")
    return 0


def fetch_for_merge(base: str, prs: Sequence[int]) -> None:
    """Fetches origin/<base> and each PR's head, so a stale or missing ref is never read as a clean merge; a failure is
    a Refused."""
    try:
        git("fetch", "-q", "origin", f"+refs/heads/{base}:refs/remotes/origin/{base}")
        for pr in prs:
            git("fetch", "-q", "origin", f"refs/pull/{pr}/head")
    except subprocess.CalledProcessError as exc:
        raise Refused(
            f"{' and '.join(f'PR #{pr}' for pr in prs)}: cannot fetch origin/{base} and the head to check the merge: "
            f"{exc.stderr.strip() or exc}"
        ) from exc


def merge_conflicts(first: str, second: str, what: str) -> list[str]:
    """The files `git merge-tree --write-tree first second` conflicts in (empty when it merges cleanly). A conflict whose
    files git doesn't name still returns one entry, and a git failure is a Refused naming `what`: never a clean."""
    merge = subprocess.run(
        ["git", "merge-tree", "--write-tree", "--name-only", "--no-messages", first, second],
        capture_output=True,
        text=True,
        check=False,
    )
    if merge.returncode == 0:
        return []
    # exit 1 is a conflict only with its tree on the first line: a commit git doesn't have exits 1 with nothing
    if merge.returncode != 1 or not re.fullmatch(
        r"[0-9a-f]{40,64}", merge.stdout.split("\n", 1)[0]
    ):
        raise Refused(f"{what}: git merge-tree failed: {merge.stderr.strip() or merge.returncode}")
    return list(dict.fromkeys(merge.stdout.splitlines()[1:])) or ["(files not named by git)"]


def conflicted(pr: int, base: str, head: str) -> list[str]:
    """The files a PR's head conflicts with its own base in, from `git merge-tree --write-tree origin/<base> <head>`
    (empty when it merges cleanly), after fetching both (fetch_for_merge), so a stale or missing origin/<base> is never
    read as clean. A conflict whose files git doesn't name still returns one entry, and a fetch or git failure is a
    Refused: never a clean."""
    fetch_for_merge(base, [pr])
    return merge_conflicts(f"origin/{base}", head, f"PR #{pr}")


def hunks(base: str, head: str, path: str) -> str:
    """The lines `head` changes in `path` since `base` (a merge-base), numbered on `base`, as `3-5, 9` from
    `git diff -U0`; a pure insertion is the one line it follows."""
    out = git("diff", "-U0", "--no-color", base, head, "--", path)
    spans = []
    for old, count in re.findall(r"^@@ -(\d+)(?:,(\d+))? \+", out, re.MULTILINE):
        start, length = int(old), int(count) if count else 1
        spans.append(f"{start}-{start + length - 1}" if length > 1 else str(start))
    return ", ".join(spans) or "(no lines)"


def overlaps(pr: int | None, files: Sequence[str] = (), base: str | None = None) -> list[str]:
    """What `overlap` prints: for each other open PR into the same base that changes a file of PR `pr` (or one of
    `files`, for a story with no PR yet, into `base`, default the default branch), its files with both sides' hunks and,
    with a PR, whether the two heads merge cleanly. The PRs and their files come from one call; only a PR that shares a
    file is fetched. Empty with no overlap; a fetch or git failure is a Refused, never a clean."""
    found = open_pull_files(OPEN_LIMIT)
    if pr is not None:
        mine = next((p for p in found if p["number"] == pr), None)
        if mine is None:
            raise Refused(f"PR #{pr} is not open")
        base, files = mine["baseRefName"], mine["files"]
    base = base or default_branch()
    sharing = [
        (p, [f for f in p["files"] if f in files])
        for p in found
        if p["number"] != pr and p["baseRefName"] == base
    ]
    sharing = [(p, shared) for p, shared in sharing if shared]
    if not sharing:
        return []
    fetch_for_merge(base, [*([pr] if pr is not None else []), *(p["number"] for p, _ in sharing)])

    def span(head: str, path: str) -> str:
        return hunks(git("merge-base", f"origin/{base}", head).strip(), head, path)

    def behind(number: int, head: str) -> str:
        # merge-tree of two heads replays the base's commits one of them lacks, so a head that conflicts with the base
        # alone shows that as a clash with the other PR too; say so, or the lead reorders PRs when the base is the cause
        clash = merge_conflicts(f"origin/{base}", head, f"PR #{number}")
        return (
            f"; PR #{number} conflicts with {base} itself in {', '.join(clash)}: merge {base} into it and rerun"
            if clash
            else ""
        )

    lines = []
    own = None  # this PR's base note, read on its first clash only, so a clean list runs no base check
    for other, shared in sharing:
        number = other["number"]
        verdict = ""
        if pr is not None:
            clash = merge_conflicts(
                mine["headRefOid"], other["headRefOid"], f"PRs #{pr} and #{number}"
            )
            if clash and own is None:
                own = behind(pr, mine["headRefOid"])
            verdict = (
                f": conflicts in {', '.join(clash)}{own}{behind(number, other['headRefOid'])}"
                if clash
                else ": merges cleanly"
            )
        lines.append(f"PR #{number} also changes {len(shared)} file(s) here{verdict}")
        for path in shared:
            ours = f"PR #{pr} {span(mine['headRefOid'], path)}; " if pr is not None else ""
            lines.append(f"  {path}: {ours}PR #{number} {span(other['headRefOid'], path)}")
    return lines


def overlap(pr: int | None, files: Sequence[str], base: str | None) -> int:
    if lines := overlaps(pr, files, base):
        print("\n".join(lines))
    return 0


def leave_own_environment() -> None:
    """Hand every command this script starts the caller's environment, not the script's own. uv puts the script's
    environment in VIRTUAL_ENV, its bin first on PATH and counts the run in UV_RUN_RECURSION_DEPTH, and a tool that
    follows VIRTUAL_ENV (a type checker) would check the project against the script's packages. A VIRTUAL_ENV equal
    to this script's own is dropped with its bin on PATH, and one that points elsewhere (the script started by an
    interpreter directly) is passed on unchanged; no value is reconstructed, so under `uv run`, which replaces the
    caller's VIRTUAL_ENV before the script starts, the caller's own value does not reach the commands."""
    own = os.path.normpath(sys.prefix)
    if os.path.normpath(os.environ.get("VIRTUAL_ENV", "\0")) != own:
        return
    del os.environ["VIRTUAL_ENV"]
    bin_dir = os.path.join(own, "bin")
    os.environ["PATH"] = os.pathsep.join(
        e for e in os.environ.get("PATH", "").split(os.pathsep) if os.path.normpath(e) != bin_dir
    )
    if (depth := os.environ.get("UV_RUN_RECURSION_DEPTH", "")).isdigit():
        if int(depth) > 1:
            os.environ["UV_RUN_RECURSION_DEPTH"] = str(int(depth) - 1)
        else:
            del os.environ["UV_RUN_RECURSION_DEPTH"]


def pack_behind(script: Path | str) -> str | None:
    """One line, or None: how many commits its upstream's last fetched tip holds that the checkout lacks and that
    changed ``script``, counted since the merge base, so a local commit to it, or upstream changes elsewhere, say
    nothing. Never fetches, so it stays fast and offline-safe; a checkout with no upstream,
    one not behind, or one whose script is unchanged upstream is None. A short copy of this lives in
    the omnigent-agents skill's omnigent_agent.py (`pack_behind`), since skills install separately and neither may import
    the other: change both together."""
    here = Path(script).resolve()

    def ask(*args: str) -> str | None:
        try:  # a probe that fails for any reason says nothing, since it must never stop a command
            done = subprocess.run(
                ["git", "-C", str(here.parent), *args], capture_output=True, text=True, check=False
            )
        except Exception:  # noqa: BLE001
            return None
        return done.stdout.strip() if done.returncode == 0 else None

    behind = ask("rev-list", "--count", "HEAD..@{upstream}", "--", str(here))
    if behind is None or int(behind) == 0:
        return None
    return (
        f"{here.name}: this checkout is behind its upstream, where {behind} commit(s) changed this script: "
        "run `git pull`"
    )


def refuses_stale() -> bool:
    """Whether the project's `pack_staleness: refuse` (loop.md's `loop` section) makes a stale pack a refusal; a
    project without settings, or outside a checkout, only gets the warning."""
    try:
        return settings(project()[1]).get("pack_staleness") == "refuse"
    except (Refused, subprocess.CalledProcessError):
        return False


# `record <kind>`: the verb each kind was before #614, which the code below names
RECORDS = {"approval": "approve", "verdict": "verdict", "checks": "local-ci"}
# today's verbs, kept for one release line (approvals.py's), and the gatekeeper's words for each
ALIASES = {verb: ["record", kind] for kind, verb in RECORDS.items()}


def aliased(argv: list[str]) -> list[str]:
    """`argv` with a verb of before #614 (ALIASES) as the gatekeeper names it; any other argv as it is."""
    if argv and argv[0] in ALIASES:
        return [*ALIASES[argv[0]], *argv[1:]]
    return argv


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)
    checking = commands.add_parser("check")
    checking.add_argument("point", choices=("build", "merge", "ready", "rerun", "setup", "stalled"))
    checking.add_argument("pr", type=int, nargs="?")
    checking.add_argument("--issue", type=int)
    # `record`: the evidence the gate reads, written: an approval, a verdict, a check pass
    records = commands.add_parser("record").add_subparsers(dest="kind", required=True)
    approving = records.add_parser("approval")
    approving.add_argument("point", choices=(*POINTS, "merge", BREAKGLASS))
    approving.add_argument("number", type=int)
    # an agent never approves: a record is a person's word, and the owner's policy in loop.md covers the rest; a
    # merge's go, and with `loop.delegate: all` a spec or plan, may be the delegated approver's, under the owner's
    # delegation (approve, delegated)
    approving.add_argument("--by", choices=("owner", DELEGATE), required=True)
    # merge only: the head the approver was shown, and their go taken back
    approving.add_argument("--head")
    approving.add_argument("--withdraw", action="store_true")
    # breakglass only: why, and when the approval lapses (ISO 8601; default two days on)
    approving.add_argument("--reason")
    approving.add_argument("--expires")
    proving = commands.add_parser("breakglass-proof")
    proving.add_argument("pr", type=int)
    proving.add_argument("--head", required=True)
    proving.add_argument("--failed-run", type=int, required=True)
    proving.add_argument("--rerun", type=int)
    proving.add_argument("--smoke", type=int)
    proving.add_argument("--earlier", type=int, nargs="*", default=[])
    delegating = commands.add_parser("delegate")
    delegating.add_argument("pr", type=int)
    posting = records.add_parser("verdict")
    posting.add_argument("pr", type=int)
    posting.add_argument("report", type=Path)
    reading = commands.add_parser("report")
    reading.add_argument("report", type=Path)
    reading.add_argument("--json", action="store_true")
    recording = records.add_parser("checks")
    recording.add_argument("pr", type=int, nargs="?")
    recording.add_argument("--record", action="store_true")
    recording.add_argument("--run", action="store_true")
    recording.add_argument("--status", metavar="HEAD")
    recording.add_argument("--head-proof", action="store_true")
    # --rerun: an independent rerun of the head's selection (levels' `rerun`): no pass reused, its record its own
    recording.add_argument("--rerun", action="store_true")
    # --no-push: with --rerun, keep the record as a local branch and name it (KEPT), for a CI job without a write token
    recording.add_argument("--no-push", action="store_true")
    recording.add_argument("--head", metavar="SHA")
    # --base: in narrowed mode, the commit `--record` selects against (land's cloud gate passes its batch's base tip)
    recording.add_argument("--base", metavar="REV")
    # --step: run and record only the named `gate_steps` step(s), an `on_demand` one included (#2295)
    recording.add_argument("--step", action="append", metavar="NAME")
    merging = commands.add_parser("land")
    merging.add_argument("pr", type=int, nargs="*")
    merging.add_argument("--wait", action="store_true")
    merging.add_argument("--only", action="store_true")
    overlapping = commands.add_parser("overlap")
    overlapping.add_argument("pr", type=int, nargs="?")
    overlapping.add_argument("--files", nargs="+", metavar="path")
    overlapping.add_argument("--base")
    graduating = commands.add_parser("graduate")
    graduating.add_argument("epic", type=int)
    graduating.add_argument("--pr", type=int, required=True)
    # --base: the commit to select against (a PR's base SHA, a push's previous tip), over the merge base
    selecting = commands.add_parser("affected-tests")
    selecting.add_argument("--base", metavar="REV")
    # --record: a pass is recorded and pushed as narrowed `local-ci --record` records one (the worker's final head)
    selecting.add_argument("--record", action="store_true")
    commands.add_parser("queue-config")
    # the batch's base: the queue's target branch tip the batch merges onto
    queueing = commands.add_parser("queue-select")
    queueing.add_argument("base", nargs="?", default="origin/main")
    rerunning = commands.add_parser("rerun-tests")
    rerunning.add_argument("failed", metavar="failed-tests.json")
    rerunning.add_argument("--red", required=True, metavar="HEAD")
    rerunning.add_argument("--base", metavar="REV")
    sharding = commands.add_parser("shards")
    sharding.add_argument("--suite", required=True)
    sharding.add_argument("--selection")
    sharding.add_argument("--whole", action="store_true")
    sharding.add_argument("--durations")
    sharding.add_argument("--passed", metavar="HEAD")
    sharding.add_argument("--command", dest="gate_command")
    sharding.add_argument("--record", metavar="HEAD")
    sharding.add_argument("--shard", type=int)
    recording_read = commands.add_parser("gate-record")
    recording_read.add_argument("head")
    recording_read.add_argument("words", nargs=argparse.REMAINDER, metavar="command")
    # every limit the policy's `timeouts` derive; --mergify renders .mergify.yml, --check refuses one out of step
    recording = commands.add_parser("durations")
    recording.add_argument("--record", metavar="FILE")
    recording.add_argument("--out", metavar="FILE")
    recording.add_argument("--setup", type=float, metavar="SECONDS")
    recording.add_argument("--report", action="store_true")
    timing = commands.add_parser("timeouts")
    timing_how = timing.add_mutually_exclusive_group()
    timing_how.add_argument("--mergify", action="store_true")
    timing_how.add_argument("--check", action="store_true")
    timing_how.add_argument("--json", action="store_true")
    wrapping = commands.add_parser("heavy")
    wrapping.add_argument("--plan", action="store_true")
    wrapping.add_argument("words", nargs=argparse.REMAINDER, metavar="command")
    args = parser.parse_args(aliased(argv))
    if args.command == "record":  # each kind runs as the verb it was before #614
        args.command = RECORDS[args.kind]
    global running, using
    running, using = True, None
    try:
        return dispatch(args, argv, parser)
    except Refused as exc:
        print(exc)
        return 1
    except subprocess.CalledProcessError as exc:
        # gh's reason, e.g. a closing line that names a number with no issue
        print(exc.stderr.strip() or exc)
        return 1
    finally:
        running, using = False, None
        # a rerun's state is this command's alone: a caller that runs main() again (loop.py, a test) starts clean
        if FRESH.get():
            os.environ.pop(RERUN_VAR, None)
        FRESH.set(False)
        KEEP.set(False)


def dispatch(args: argparse.Namespace, argv: list[str], parser: argparse.ArgumentParser) -> int:
    """The verb `args` names; a refusal raises."""
    if stale := next(filter(None, map(pack_behind, RUNNING)), None):
        print(stale, file=sys.stderr)
        if refuses_stale():
            return 1
    if args.command == "report":  # reads a local file only, so no remote needs choosing
        return report_json(args.report) if args.json else report_check(args.report)
    if args.command == "affected-tests":
        return affected_tests(args.base, args.record)
    if args.command == "shards":
        return sharded(args)
    if (
        args.command == "queue-config"
    ):  # the checkout's own policy, so a PR's change to `land` renders with it
        print(queue_config(), end="")
        return 0
    if args.command == "rerun-tests":
        return rerun_tests(args.failed, args.red, args.base)
    if args.command == "durations":
        return durations_verb(args.record, args.out, args.setup, args.report)
    if args.command == "timeouts":  # the working tree's files only: no forge, no remote
        return timeouts_verb(args.mergify, args.check, args.json)
    if args.command == "queue-select":
        return queue_select(args.base)
    if args.command == "heavy":  # a host's command, run in any directory: no repo or project needed
        if args.plan and args.words:
            raise Refused(
                "heavy --plan takes no command: it prints the host's slot count and runs nothing"
            )
        return heavy(["--plan"] if args.plan else args.words)
    if (
        args.command == "check" and args.point == "setup"
    ):  # reads the policy itself, to list it beside the rest
        return setup()
    one_repo()
    if args.command == "check":
        if args.point == "stalled" and (args.pr is not None or args.issue is not None):
            raise Refused("check stalled takes no PR: it lists every open PR")
        if args.issue is not None and (args.point != "build" or args.pr is not None):
            raise Refused("--issue goes with check build and no PR number")
        return check(args.point, args.pr, args.issue)
    if args.command == "breakglass-proof":
        return breakglass_proof(
            args.pr, args.head, args.failed_run, args.rerun, args.smoke, args.earlier
        )
    if args.command == "approve" and args.point == BREAKGLASS:
        if args.by != "owner":
            raise Refused(
                "approve breakglass is the owner's alone: `--by owner`, and `loop.delegate: all` never covers it"
            )
        if not args.head:
            raise Refused(
                "approve breakglass needs --head <full-sha>: the head the owner was shown"
            )
        if not args.withdraw and not args.reason:
            raise Refused("approve breakglass needs --reason <one line>")
        return breakglass(args.number, args.head, args.reason or "", args.expires, args.withdraw)
    if args.command == "approve":
        if args.point != "merge" and (args.head or args.withdraw):
            raise Refused("--head and --withdraw belong to `approve merge`")
        if args.point == "merge" and not args.head:
            raise Refused("approve merge needs --head <sha>: the head the approver was shown")
        return (
            go(args.number, args.head, args.by, args.withdraw)
            if args.point == "merge"
            else approve(args.point, args.number, None, args.by)
        )
    if args.command == "delegate":
        return delegate(args.pr)
    if args.command == "gate-record":
        return gate_record(args.head, args.words[1:] if args.words[:1] == ["--"] else args.words)
    if args.command == "local-ci":
        if args.status is not None:
            if args.pr is not None or args.record:
                raise Refused("local-ci --status takes a head alone")
            return local_ci_status(args.status)
        if args.run and args.pr is None:
            raise Refused("local-ci --run goes with a PR: --record already runs the gate")
        if (args.pr is None) != args.record:
            raise Refused(
                "local-ci takes a PR number, or --record to record a run on HEAD before one exists"
            )
        if args.head and args.pr is None:
            raise Refused("local-ci --head goes with a PR: --record gates HEAD itself")
        if args.base and not args.record:
            raise Refused(
                "local-ci --base goes with --record: the base a narrowed run on HEAD selects against"
            )
        proof = None
        if args.head_proof and (proof := head_proof(project()[1])) is None:
            raise Refused("loop.md sets no `head_proof`: --head-proof has no status to publish")
        if args.head_proof and not args.record:
            raise Refused("--head-proof goes with --record: the head proof is the checkout's")
        if args.rerun and (not args.record or args.head_proof):
            raise Refused(
                "--rerun goes with --record alone: it reruns the selection on the checkout's HEAD and records it"
            )
        if args.no_push and not args.rerun:
            raise Refused(
                "--no-push goes with --rerun: only a rerun's record is kept for another job to push"
            )
        KEEP.set(args.no_push)
        if args.step:
            asked_steps(args)
        held = ASKED.set(tuple(dict.fromkeys(args.step or ())))
        try:
            return (
                local_ci(args.pr, proof, args.run, args.head, args.base, args.rerun)
                if leads_its_session()
                else detach(argv)
            )
        finally:
            ASKED.reset(held)
    if args.command == "land":
        if args.only and not args.pr:
            parser.error("land --only needs a PR: there is nothing to restrict to")
        return land(args.pr or None, args.wait, args.only)
    if args.command == "overlap":
        if (args.pr is None) == (args.files is None):
            raise Refused("overlap takes a PR number, or --files <path>... for a story with no PR")
        if args.base and args.pr is not None:
            raise Refused("--base goes with --files: a PR's own base is the one compared")
        return overlap(args.pr, args.files or [], args.base)
    if args.command == "graduate":
        return graduate(args.epic, args.pr)
    return verdict(args.pr, args.report)


def asked_steps(args: argparse.Namespace) -> None:
    """Refuse a `local-ci --step` that the gate can't run by name: it goes with `--record` alone (no PR, head proof,
    rerun or base), and each name must be a step of loop.md's `gate_steps` (#2295)."""
    if not args.record or args.head_proof or args.rerun or args.base:
        raise Refused(
            "local-ci --step goes with --record alone: it runs and records the named gate steps on HEAD"
        )
    names = [step["name"] for step in gate_plan(every=True) if not step["legacy"]]
    if unknown := [name for name in args.step if name not in names]:
        raise Refused(
            f"local-ci --step {unknown[0]}: loop.md's `gate_steps` has no step of that name"
            + (f" (its steps: {', '.join(names)})" if names else " (it sets no `gate_steps`)")
        )


def cli(argv: list[str]) -> int:
    """The command line's run: main(), after leaving this script's own environment, recording a detached run's end."""
    leave_own_environment()  # before any command it starts
    detached = os.environ.pop(
        DETACHED, None
    )  # set by detach() for the run it starts, and for no process under it
    code = main(argv)
    if detached:
        finished(detached, code, RAN.get("command"))
    return code


if __name__ == "__main__":
    sys.exit(cli(sys.argv[1:]))
