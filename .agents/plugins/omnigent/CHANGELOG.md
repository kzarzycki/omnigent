# Changelog

## Unreleased

New entries are files in [changelog.d/](changelog.d/), one per issue, until a release moves
them here.

## 0.1.9 - 2026-11-24

- The supervisor reads a registered PR wait through ETag reads (the pull, its check runs and its combined status), so a poll of an unchanged PR answers 304 and spends no quota, and a restart's backfill reads each PR once however many waits name it. Part of #2068.
- `omnigent_agent.py supervise` wakes the notified session only for turn ends it must act on: a `DONE` whose result the lead sent to that session (a send carrying the result's first line) or whose session it already archived is told to the leads waiting on it but queues no `act` line, a turn end showing only progress or a named wait stays recorded, and the "no progress (quota)" line is said once until GitHub's quota resets (#2090).
- `wait-until` refuses a command that polls one PR through `gh` (exit 2) and names `wait-for --pr <n>`, whose ETag reads spend no quota while the PR doesn't move (#2115).
- `supervise` reads a turn's ending as its first line that opens with `DONE`, `USER NEEDED`, `PARKED` or `STILL WORKING` (`USER NEEDED` anywhere still wins), so a manager's status block that opens `STILL WORKING` and lists a `DONE:` section no longer routes as a delivered `done`; a new `Waits: own supervisor` footer (`wait-on own`) says a manager's own supervisor wakes it, and routes `record` (#2135).
- `omnigent-agents start` on `claude-cloud` now sends no permission mode unless `--permission-mode` names one, so update the Omnigent server to one with agent default modes first; the client-side "start refused … bypassPermissions" message is gone (#2148).
- `supervise` raises one `deadline` per declared wait: a wait that names sessions is keyed by its text and the sessions it names, so a reply that restates it starts no new chain, a changed wait does, and a wait still stuck after 4x `DEADLINE_SECONDS` fires once more. No `deadline` while every session the wait names ended its turn `PARKED`; its news line still tells the waiter (#2150).
- `supervise` routes a `USER NEEDED` turn end with no card open (`unasked`) to its manager as `act`, never to the owner and without the small model: the manager asks on its own card a question that spans projects, or sends the lead to its card. The lead preamble tells a lead to ask on its own card (#2170).
- `supervise` says a lead's question card still unanswered 4 hours after it first saw it (`CARD_REMIND_SECONDS`) to the lead's manager once, as a `card-waiting` event routed `act`, so a manager the owner told to act in their name can answer it; a lead the owner drives directly records it (#2170).
- The `loop.delegate: all` lead brief no longer says never to run `approve spec`: once the owner approves on the lead's own card, the lead records it with `--by owner`, as lead.md step 1 says (#2170).
- The chief of staff's console adopts a live top-level lead that no registry holds: it writes the lead a `follow` line and posts one `adopted` event, so a lead started without a registry no longer stalls unseen. `start --top-level --role lead` with no registry now exits 2 before any session exists (#2181).

## 0.1.8 - 2026-11-23

- The supervisor no longer reports a PR the merge queue holds as stalled. A `check stalled` line is checked against the PR's live state before it goes out, and dropped when the PR merged, closed, moved to a new head or is held by the queue since the sweep read it, which may be 15 minutes earlier. A PR carrying `loop:queued` whose queue check on its head is still running is waiting, not a `pr-stall`, however long the queue takes (#2100).

## 0.1.7 - 2026-11-23

- omnigent-agents: `send` and `relay` take `--expires <minutes>` (default 30, 0 = never) and tag their message with that expiry and the sending session, so one still queued behind a busy turn after its sender is gone is dropped instead of landing out of context (#909).
- `claude-cloud-env <checkout>` checks the environment the checkout pins (`.claude/settings.local.json`'s `remote.defaultEnvironmentId`, else its main checkout's) before it makes or updates one: a pinned environment named otherwise than loop.md's `Cloud worker: …, env <name>` (else the definition's name, else the repository's) fails naming both, as does a pin the login has no environment for, where it used to create a second, empty environment named after the repository beside the pinned one (#2078).

## 0.1.6 - 2026-11-23

- An archive (`PATCH /v1/sessions/<id>`) waits up to `ARCHIVE_SECONDS` (120 s) rather than the 30 s every other call gets, since a claude-cloud session's archive returns only once its bridge has archived the claude.ai session, which ends the VM and can take about 65 s; a slow archive was reported as `finish failed` (#2060).

## 0.1.5 - 2026-11-23

- omnigent-agents: `--role` takes `lead`, `worker` or `verifier` as well as a file path, naming the pack's engineering-loop role file, so a cloud child needs no role file in its launch dir (#1868).
- `start --top-level --host <other>` takes the report as that host's absolute path with `--pr` too, as without it: a relative one is refused and an absolute one is no longer created or resolved here, which failed with `PermissionError` when this host could not make that folder. `brief` does the same, and `wait --pr` on a session on another host writes the PR comment to `tmp/loop/<name>` here, where `send --pr` reads it as the report before (#2028).
- A cloud worker, verifier or gate child (`acp:claude-cloud`) whose result `run`, `wait` or the supervisor took is archived even while background tasks still run in its VM, since those pollers keep spending the account's GitHub quota; its line names how many ended with it. A lead, a manager, a top-level session with no role, a `--keep` child, an open card or prompt, a blind `reap` and every host session keep today's holds (#2052).

## 0.1.4 - 2026-11-23

- omnigent-agents: every cloud session's preamble installs gh-quota's logging gh shim from the pack clone and ends its report with its `gh_quota.py counts` and a count of its `curl` calls to `api.github.com`, since its log stays in the VM (#1994).
- omnigent-agents: `wait-for` and `supervise` resume up to 3 minutes after a GitHub rate-limit reset at random, so every poller that waited on one reset does not resume in its first second (#2000).

## 0.1.3 - 2026-11-23

- orchestrating-agents: `supervise --registry` runs the gatekeeper's `check stalled` at most once an hour for each repo its followed leads work in, detached from its poll, and says each stalled PR once per head as a `stalled` event, which `--console` routes to the chief of staff; a sweep that judged none of a repo's PRs is said once too (#1746).
- omnigent-agents: `supervise --registry` watches every open, non-draft PR of the repos it follows (and each `--stall-repo`) and wakes the manager with one `pr-stall` act event when a PR has had no push, verdict, go or merge-queue step for `--stall-minutes` (default 60), naming its blocker: a red required check, a conflict, a verdict without a go, a card waiting. It replaces the per-lead verdict-stall wake, which is now one blocker of this check (#1819).
- omnigent-agents: `start claude-cloud --host` on a bridge whose `~/.claude.json` is over 64 KB (the host files API answers 413) prints a warning that folder trust was not checked and goes ahead, instead of refusing (#1870).
- omnigent-agents: `relay` and `send` check a message for an off-request issue only when it names one: a message that names no issue reads nothing from Omnigent, so it no longer costs a session read and the follow-up path `test_a_failed_line_is_no_report_to_the_supervisor_a_start_a_follow_up_or_a_rerun` covers passes again (#1892).
- omnigent-agents: `start --host <remote> --workspace <path> --pr N` reads the workspace's `origin` through the host file API to name the PR's repository, and refuses with `--pr owner/repo#N` when it cannot, instead of labelling the current directory's repository (#1902).
- omnigent-agents: `omnigent_agent.py wait-for --pr` and the supervisor's required-check read spend no GitHub quota on a round where nothing they read changed: every REST read carries its last ETag (a 304 is free), and `--until checks|merged|closed` decides from those REST answers alone, with no GraphQL read (#1984).
- omnigent-agents: a cloud child's brief says where the pack's scripts come from: a clone of the pack's latest release tag, resolved in the VM with `git ls-remote` (no API quota), or the tag the brief pins with a `Pack: engineering-vX.Y.Z` line, never the checkout's vendored copy, whose pin may be releases old (#1987).

## 0.1.2 - 2026-11-22

- omnigent-agents: the cloud brief waits on a run with one tracked `wait-for --budget <the run's expected length>` instead of a `--budget 480` loop, and `wait-for --budget`'s help says its default is for a foreground call (#1790).
- omnigent-agents: `supervise --console` wakes a followed lead quiet an hour whose PR has a verdict on its current head over an hour old with no push or go since, with one `news` line per PR and head naming the step it owes (#1802).
- omnigent-agents: `start` refuses a second worker or verifier of a PR while one with the same role and PR runs, and `reap --force <id>` stops and finishes that running one, its open PR included (#1806).
- omnigent-agents: `relay` and `send` to a lead print a warning, and still deliver, when the message names an issue outside the lead's request, its epic or the epic's sub-issues (#1830).

## 0.1.1 - 2026-11-22

- orchestrating-agents: a session whose result is taken is archived at one shared point, `finish_delivered`: `run`, `wait`, `reap`, the supervisor's relay of a child's `DONE` report or failure and of a followed lead's `DONE` all call it, and a kept session (`--keep`, an open card, a PR hold, background work) stays open; `start --keep` labels a session so the supervisor leaves it open; a run that ended failed, cancelled or errored is finished too, a stale `omnigent.keep` label holds nothing, and archiving a parent finishes its idle children (#1579).
- omnigent-agents: `supervise --console` acts on a `STILL WORKING` that nothing wakes or that hands work back (#1753). A wait on a process the harness does not track ("waiting on pid 4242", "its pid is 4242", a pid file or a log or output file, with no `--notify`, `LOOP_NOTIFY`, child, CI or `Waits:` line), or in a cloud session on a background subagent, gets one nudge (`LEAD_UNTRACKED`) saying why nothing wakes it and how to wait instead, then act if the lead ends that way again; it used to record the turn as a named wait and leave the lead idle until the no-progress deadline. A hand-back, a `Handback: #<n> <what>` line or prose such as `#12 READY` or `#12 ... is yours`, goes to the session it notifies at once.
- omnigent-agents: `ssh` runs only short read-only checks and setup steps (#1754).
- omnigent-agents: a cloud worker's preamble no longer tells it to leave the session link out of its commit messages (#1764).
- omnigent-agents: Host steps over SSH points to orchestrating-agents' rule *Host work* instead of repeating it (#1765).
- omnigent-agents: a follow-up run updates a PR branch by a merge of the base, not a rebase (#1768).

## 0.1.0 - 2026-11-22

- omnigent: the first release, split out of `engineering`. It holds `omnigent-agents` (the Omnigent backend, formerly orchestrating-agents' `backends/omnigent.md`, and its driver `scripts/omnigent_agent.py`), `operating-omnigent` and `omnigent-telemetry`, with the cloud launcher `claude_cloud.py` and its client `claude_environments.py`, which `orchestrating-agents` held. It gains its `engineering` dependency when `engineering` drops its dependency on `omnigent` (#615).
- The full test suite is green again after 0.34.1: the remote-loop fake answers `GET /v1/hosts`, the cloud-floor start pins its launch dir, the picked-host start writes its report under a local folder, and the installed-pack settings test skips a setting the pinned release predates (#1701).
