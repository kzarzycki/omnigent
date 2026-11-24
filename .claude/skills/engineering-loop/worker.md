# Worker

You implement one item of a spec, or a list of findings, on your own branch. The
lead's message gives the spec, the item, the PR, worktree, branch, notes path and the tests to run.
Read the notes, the spec, [coding-standards.md](coding-standards.md) and the project's
`docs/agents/coding-standards.md` first. Run every command in your worktree, never in the
main checkout. A lead that builds an item itself follows this file on its own PR
branch: it pushes that branch, merges nothing, and writes the report into its notes.
A worker in a cloud session pushes to the `claude/…` branch its session started on, not to a `<branch>` a brief names, and reports that branch and its head SHA (SKILL.md, Cloud worker).
Where your build, test and proof runs go is SKILL.md, Where heavy steps run; a worker in a
cloud session (the default unless loop.md's `Cloud worker:` line says `none`) follows SKILL.md, Cloud worker.

1. **Build**, with the Build skill when gatekeeper.md's `practice` names one, under these rules.
   On a branch with no PR yet, run `approvals.py check build --issue <n>` first (the
   project's `loop:approvals build` reads the issue from a branch named `<prefix>/<n>-…`)
   and build only once it exits 0, since without a PR nothing else checks that the spec
   was approved.
   A project's or owner's simplification rule (YAGNI, `ponytail:`) applies to how a spec
   item is built, never to whether: narrowing or dropping an acceptance item or an
   Implementation Decision is a question to the lead in `<report>.question`, and that item
   stops until it is answered, since only the owner changes an approved spec's scope.
   Read the spec's comments as well as its body, since a later comment can narrow
   the spec. A brief with "the verifier will check" lists the checks the verifier will run on
   your head: run each on your own head before you report, and list each one's result in the
   report, since a check left to the verifier costs a fix round and another pass.
   For each scriptable Before merge line of the spec, write the check it names and commit it
   in the PR: in the project's suite where loop.md's `Acceptance tests:` line is set, else as
   an executable file in `acceptance/<issue>/` (SKILL.md, Acceptance checks), so the example
   reruns after the merge; a `Manual:` line gets the evidence it names instead.
   Fixtures are synthetic: nothing in `docs/agents/issue-tracker.md` § Never on GitHub
   goes into code, tests or commits. A changed rule gets its line
   where the project records rules. Review angles you want run as native subagents.
2. **Gates and push.** A check runs only if it can fail because of the change (SKILL.md, A check runs only if it can fail because of the change): run nothing whose result a passed run on the same inputs already gives, and the push hook's run is the run you report: don't run the affected tests a second time beside it, since the hook runs the same selection on the same head. Iterate on the narrowest check that covers the change: an incremental build of the touched
   target, then the tests of the module you changed, since a whole-suite run can take twenty minutes to show a compile
   error a build shows in one. The run you report is the tests the PR affects, on every head you push, and
   never the whole suite. A fix round selects against the last head its run passed on, not the PR's base
   (SKILL.md, A check runs only if it can fail because of the change, R4): `approvals.py affected-tests --base <the last head its run passed on>`.
   Where loop.md has an `Affected tests:` line, `uv run
   <gatekeeper>/scripts/approvals.py affected-tests`, which runs that selection against `git merge-base origin/<base>
   HEAD` on a first push (the base of the branch's open PR, else the default branch; a shallow clone is deepened) in the variable
   `Gate base:` names (else `GATE_BASE`), under the host gate slot; without the line, `mise run test:changed`. A
   selector that can't tell which tests a change touches runs nothing and says so: report that line as it is, and
   never fall back to the whole suite. You run no full `mise run check`, and no gate of record but the one
   `local-ci --record` on your final head (Report), with `ci: false` too: no other gate runs on the PR before it
   merges, and the nightly runs the full suite (SKILL.md, Merge model). Lint runs at commit, in the hook's `lint:` tasks on the staged files.
   Write the e2e tests that prove the change works, in the PR, and name them in the report: the verifier proves
   the change with those tests and runs no suite of its own, so a behaviour no e2e test of yours reaches is one
   nothing runs before the merge. Where the project has no e2e tier, the acceptance checks of step 1 are those tests.
   A test that passed only on its retry, or failed in a
   file the change doesn't touch, follows SKILL.md, Flaky tests, and goes in the report with
   the run it came from (a CI run's id, else the time the run started), since that run and
   the head are what make it one occurrence.
   When your brief opens with a `LOOP_HEAVY_LOCK=<path>` line, run each test run
   under that lock, around the host's heavy wrapper (SKILL.md, One heavy gate per host slot; `affected-tests` takes the slot itself, so the lock is for `mise run test:changed` and other bare commands), since your lead may run another
   worker beside you and the host's slot counts every caller: `perl -MFcntl=:flock -e '$^F =
   1023; open(F, ">>", shift) or die "$!\n"; flock(F, LOCK_EX) or die "$!\n"; exec @ARGV'
   <path> uv run <gatekeeper>/scripts/approvals.py heavy mise run test:changed`, or, with an `Affected tests:` line, `<path> uv run <gatekeeper>/scripts/approvals.py affected-tests` (perl, since macOS has no `flock`).
   Once it exits 0, commit and push with `git push -u origin <branch>` as
   its own command, since a pre-tool hook may scan the whole command text. Commit and push
   after each step whose tests are green, not only at the end, so a worker killed mid-task
   leaves its work on the branch. A fix round (a brief of a verdict's findings) runs the same:
   once the affected tests and the mutation check (Report) pass, commit and
   push and report the head, since the lead starts the next verifier on it. When the
   brief says the verifier reviews in your worktree (lead.md step 7,
   Shared worktree), run nothing there beside the pass: run the affected tests and the
   mutation check, push and stop, since the verifier's mutation patches edit that tree.
   The lead merges your branch and opens the PR: a
   brief that asks for a PR means push the branch and report it. A spec or plan the brief
   asks for goes in a file under `tmp/loop/`, and you report its path: the lead
   posts it, since the spec lives on the issue. Your job ends at the push, and once the head is handed to the queue or for approval the branch is frozen: PR events (review comments, checks) are read-only to you, and a follow-up goes in a new PR. Don't wait for
   CI. A brief that is a failing check log (a merge queue's batch check) is a check red after the PR went ready: fix it in
   the worktree you are given, then run the affected tests above and push the same way. A command that
   may outlast one tool call (a test run, a held lock, a queue) starts detached (verifier.md, A long or locked run is detached),
   with its log, pid and exit code in files, and is waited on with one wake, as that section says: one
   `wait-for --log <log> --pid <pid> --budget <the run's expected length>` (a PR's checks: `wait-for --pr <n>`) as a
   background call your harness tracks, never a short `wait-for`, `until grep` or `sleep` loop, since each return
   re-reads your whole context; never end a turn waiting on a detached run alone, since nothing wakes you and the lead reads silence as work. A `stalled:` exit (exit 1, the log silent for `--stall` seconds, the run's process group killed) means the step hung, not that the change failed: report it with the log tail, and don't rerun it unchanged.
   Any other pending condition (a queued run, a free device, an unlocked screen) is waited on the
   same way, in the foreground, with `scripts/omnigent_agent.py wait-until
   --every <s> --limit <s> -- <check>`, each call under the tool timeout: the
   turn goes on once it holds, and past the limit you state it ends with a report naming the
   condition, since a background command's exit does not wake an idle session.
3. **Trail.** Fixing findings from a PR review, make each fix its own commit. After the
   push, reply on each finding's thread with its commit SHA and resolve it, reason
   `ADDRESSED` (github.md, Review trail). Resolve only a thread you fixed: deferring or
   rejecting a finding is triage, the lead's.
4. **Report.** Every log or capture you hand over as proof (a test run, a repro, a drive
   of the instance) starts with the output of `git rev-parse HEAD` and the command that
   produced it, since a proof of another commit says nothing about the one under review.
   That SHA is the whole provenance: a checksum manifest or digest exchange adds nothing a
   commit does not already pin. A fix that changes code a proof exercises reruns that proof
   before the report, which lists each proof log with the SHA it names.
   On the final head, run `approvals.py local-ci --record` once (`--record --head-proof` where loop.md configures a
   head proof, which covers the PR's acceptance classes): it runs the gate and pushes its pass as a record keyed by
   tree and command, which a ready PR's CI and the verifier read, so neither runs it again on this tree (SKILL.md, A
   check runs only if it can fail because of the change, R1). Run the acceptance command on the head (SKILL.md,
   Acceptance checks) once, into a log stamped that way, and hand it over as the spec's proof. Just before the report, run `git status
   --porcelain` and commit or remove what it lists, and the report carries the line `git status
   --porcelain: empty`, since the verifier may review in that tree and an uncommitted check
   proves nothing on the head.
   A change to a web UI gets a rendered check, since passing tests say nothing about what
   the page shows: the project's own browser e2e when it has one, else `uv run
   <browsing>/scripts/browse.py <url> --out <png>` (`--do <file.py>` to click, hover or assert first,
   `--omnigent` to sign in to an Omnigent server; its docstring says what a sandbox needs).
   Every host with Playwright's Chromium in `~/.cache/ms-playwright` can render headless;
   hand over the PNG paths.
   Run browser tests and drives headless: a window that opens on the owner's machine takes
   focus from what they are doing. Start a headless browser of your own rather than attaching to one already open there
   (over CDP or a remote-debugging port), since that one is headed; attach only when the
   check needs that browser's logins, and say so in the report. Where a test can't run headless,
   open the browser in the background without taking focus and say in the report which
   test needed it and why. A project whose e2e opens a headed browser by default names the
   headless switch in its `docs/agents/` files.
   Before the report, mutation-check each production change you add: one patch per
   condition, branch or value, written as verifier.md, Mutation-check the new tests, says,
   committed with its tests first, since mutate.py refuses a tree with uncommitted changes, and
   run with `uv run <this skill's folder>/scripts/mutate.py --test '<tests>' --log <log>
   <patch>…`, which queues on the host gate slot itself (the short lane when the tests pass on the head within
   `short_seconds`: SKILL.md, One heavy gate per host slot), so the test command takes no lock of its own. A survivor
   gets a test before the report, which lists the log of the run with none left, since the
   verifier runs the same tool and a survivor there costs a pass.
   Clean up what you started (servers, temporary files), and only that: stop a process by
   the pid or process group you saved at its launch, or by a pattern holding your own
   worktree path once `lsof -a -p <pid> -d cwd` shows each match runs there, never with
   `pkill -f` or `killall` on a pattern other agents share, since on a shared host it kills
   their runs too. Then write your
   report to a file and send the lead its path: the branch and head SHA, the PR, the test
   runs' exit codes, the e2e tests you wrote for the verifier, your signature (what orchestrating-agents' `scripts/omnigent_agent.py signature` prints, else
   the `AGENT_SIGNATURE=` line atop your brief), the line `First commit: <n> s after the brief` (the brief file's time to the first commit's committer date, `unknown` when no commit is made), the line `Spec deviations: none` or that line
   with one bullet per deviation quoting the spec line it narrows (each one already asked,
   above), anything else left out and why, and what you tried and dropped. The deviations
   line is fixed, so a narrowed item never sits among harmless choices where the lead skims
   past it. A follow-up starts fresh from this report, not your transcript. If you're
   blocked, report that and stop. Blocked on a step only the owner can unblock (SKILL.md, Owner
   steps park), first stop that step and release what it holds, then report the exact owner
   action, since the lead parks it and nothing else frees the slot. A tool call held for approval waits for the owner, not a hang: never make its change by another route, since the approval is theirs to give (SKILL.md, Owner steps park).
