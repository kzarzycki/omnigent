---
name: agent-hygiene
description: "Keep a dev account where coding agents work clear of what rebuilds itself (merged worktrees, tool caches, idle environments, dead Omnigent terminals) with the agent-hygiene script, settle the routine cases it keeps, and pass on only what needs the owner. Use for a scheduled hygiene round, or when an account runs low on disk or pseudo-terminals."
metadata:
  requires: "gh lsof"
---

# Agent hygiene

`scripts/agent-hygiene` clears what rebuilds itself on one account and lists what it kept.
It deletes only with each tool's own command, and leaves anything in use alone. Run it
from a shell; `--help` lists its flags, and its docstring is the short version of this file.

## A round

A scheduled round runs in one conversation that every run continues, so it can compare
with the last round:

1. Run `~/.local/bin/agent-hygiene --hold-asks`. It clears, posts a low-disk warning, a
   stale pack and drift to the chief of staff as usual, and prints each question as an
   `ASK:` line instead of posting it.
2. Settle the routine `ASK:` lines yourself (Settling what it kept). Leave the rest. When it
   printed a `CARD:` line (Idle sessions), post that JSON once as `sys_ask_user_question`'s input.
   Every run until the next sweep prints the same card again (`~/.local/state/agent-hygiene/card`),
   so it is never lost to a rerun; post it only when this conversation has not posted that card yet.
3. When an `ASK:` line is left, run `~/.local/bin/agent-hygiene` once more: what you
   settled is gone, so it posts only the rest, each question once.
4. Reply with the exit code of the last run and what changed since the last round:
   what you settled, a new finding, a finding gone. A finding the last round already
   reported is not reported again, since the chief of staff has it.

A round outside a schedule (a low disk, a pty pool near its limit) is the same steps.

## What it clears

- **Caches:** `uv cache prune`, `pnpm store prune`, `npm cache clean --force`,
  `go clean -cache`, `pre-commit gc`, `brew cleanup --prune=all -s` (only in an account
  that can write both the prefix's `Cellar` and its `var/homebrew/locks`, since cleanup
  takes a lock there), `docker builder prune -f --filter until=24h` (build cache
  no build used for a day; images, containers and volumes stay; skipped when no Docker
  daemon answers). A tool that isn't installed is skipped. A tool with a running process
  is skipped, and one still running after its timeout is stopped and logged as in use:
  `uv cache prune` waits for every other uv process, so a long `uv run` turns it into a
  skip, never a wait, unless every holder of the cache's `.lock` is a `uv run` or `uvx`
  older than the wait, and neither it nor a program it started has an open file, a command
  line or an environment (`VIRTUAL_ENV`, `PATH`, `PYTHONPATH`) naming a path under the cache dir: the prune is then retried
  once with `--force`, each holder logged (pid, start time, command); any other holder, one
  running from the cache, or one whose files or environment cannot be read keeps the skip and
  is named in it. The guard cannot see a program launched with no cache path anywhere in its
  files, command line or environment. A uv cache shared with another account, which owns part of it, stops
  the prune at that account's files (`Permission denied`): the round doesn't fail on it, and
  tells the chief of staff once, naming the cache folder and how to share or split it; it
  changes no ownership or permissions itself.
- **Worktrees** under `~/dev` whose branch has a merged PR on the GitHub origin (or,
  detached, whose HEAD is the head of a merged or closed PR), and the worktrees with no PR
  of their own inside one (a worker's or agent's), judged with that PR and removed first.
  One goes with `git worktree remove` (which keeps the local branch) when all of these hold:
  - `git status` is clean apart from untracked `*.lock` files (also inside an untracked
    directory that holds nothing else);
  - HEAD is on a remote branch, or is the PR's head (same commit or tree), or in it;
  - a detached worktree's HEAD is no remote branch's tip (a landing checkout parked at the
    default branch's tip is live, and a landed PR's head is that tip), and none of its own git
    dir's `HEAD`, `index`, `logs/HEAD` or `FETCH_HEAD` (a checkout touches the first three,
    a fetch the last) has changed in 24 hours, a worktree used lately being kept without
    a question;
  - the worktree isn't locked (`git worktree lock`);
  - no process has its cwd or a file in it (a held lock file included) or names it;
  - its `tmp/` and `.bootstrap-state/` (run reports, ignored or untracked) haven't changed in
    24 hours, since a lead may still read a report there over ssh, where the process scan
    can't see it; stale untracked ones go with the worktree, file by file beside a tracked
    `tmp/.gitkeep`;
  - every ignored file in it is one of: an environment; build output (also a SwiftPM
    `.build/`, a directory holding `CACHEDIR.TAG`, a file whose first line says it is
    generated and not to be edited, an `*.xcodeproj/` beside the `project.yml` XcodeGen
    rebuilds it from); a `*.log`; test output (`test-results/`, `playwright-report/`,
    `snapshot_failures/`); a built `static/web-ui/`; a file byte-identical to the main
    checkout's copy at the same path; a `.terraform/`; an ignored directory holding no file (only empty directories); `.apm-local/`; a `.claude/settings.local.json`
    holding only the cloud-environment pin (`remote.defaultEnvironmentId`); anything under
    `logs/`, `tmp/` or `.bootstrap-state/`; or what `apm.lock.yaml` lists as deployed by
    `apm install`. `git worktree remove` deletes ignored files, so a `.env`, a hand-written
    skill or a nested worktree keeps the worktree; a clone SwiftPM keeps under
    `.build/checkouts/` does not.

  A kept worktree's log line names every path and reason that holds it.
- **Environments:** `.venv` and `node_modules` beside their manifest, in a checkout under
  `~/dev` with no process in it and no change for 14 days (`--idle-days`).
- **Dead Omnigent terminals:** this user's `omnigent/terminals/program_status.py` wrappers
  with ppid 1 whose `--bridge-dir` directory no longer exists. They leak ptys (macOS has
  511 machine-wide; at the limit every new session fails at launch). The wrapper ignores
  SIGTERM, so it gets SIGTERM, then SIGKILL after 3 s, on its process group; launchd reaps
  the zombies. A wrapper with no `--bridge-dir`, or whose directory exists, is left alone.
  The log shows ptys and zombies before and after, and `WARN` lines when zombies stay
  above 20 or ptys exceed 80% of the limit.

- **Idle sessions**, at most once a day (`~/.local/state/agent-hygiene/swept`; the other
  rounds of the day skip it): every unarchived Omnigent session idle (not updated) for over a
  day is archived through `omnigent_agent.py sweep`, which finishes each with
  `finish_delivered`, the function the `DONE` path uses, so a failed, cancelled or errored
  session counts as finished. Each session is judged on its own, so archiving a parent never takes
  a child that is kept, talked to or not idle. Kept: a running session,
  one with an open question card, a pending prompt, background work or input pending, the chief
  of staff and project managers (`omni.role` `cos`, `pm`), this session, a `start --keep`
  session (`omni.keep`), a worker whose PR is open, and each session on the keep list. An
  `omnigent.keep` label (set when a round needed the owner, cleared by nothing) keeps a session
  only while its reason stands, an open card or one of the holds above; on its own it is stale
  and holds nothing. A session the owner talked to directly (a message they typed, not a brief,
  relay or post) is never archived by the sweep: they all go on one owner card, printed as a
  `CARD:` line (`sys_ask_user_question` input): one multiSelect question listing them,
  "Archive all" recommended, a "Keep" option per session for the exceptions. Whoever posts it
  archives the others with `omnigent_agent.py reap <id>...` once the owner answers. Without the
  omnigent-agents skill in the home skill install the sweep is skipped; a sweep that fails fails
  the run, and the next round sweeps again. `--dry-run` says what it would archive.

  **The keep list** is `~/.config/agent-hygiene/keep`, beside the hygiene state, not code: one
  session id or exact session title per line, `#` starting a comment; an entry matches a whole id
  or title, never a prefix. A keep list that is there but can't be read (a directory, no
  permission) fails the sweep, which then archives nothing. A session on it is never
  swept, however long it is idle; take the line out to let the sweep have it.

If `lsof` or `ps` fails, nothing is removed and the run exits 1. Agent state (`~/.claude`,
`~/.codex`, `~/.omnigent`), user files, simulator devices, VM and container images are
never touched. `--dry-run` lists what it would remove and the message it would post.

## Settling what it kept

The script asks about what it kept but may not be wanted. Settle a case the way the owner
would, when the answer doesn't depend on what they want:

- **A merged worktree with uncommitted work or an unpushed HEAD.** Look at what holds it
  (`git status`, `git log @{upstream}..` or the commits past the PR's head). When the work
  is already on the default branch (the same change landed) or is scratch (debug prints, a
  reverted edit, generated output), it is not wanted. Otherwise keep it reachable: commit
  any uncommitted work there (`git add -A && git commit -m 'hygiene: kept work'`), then
  `git tag hygiene/<branch or short sha>-<YYYYMMDD>` on that commit. A local tag costs
  nothing and keeps the commits from garbage collection, so nothing is lost. Then
  `git worktree remove --force <path>` from the main checkout. The reply names each tag.
- **A worktree with no PR of its own** inside a merged one: the same, judged with that PR.

Leave to the owner, since only they know whether it is wanted: ignored files that may not
rebuild (a `.env`, a hand-written skill), a locked worktree, a worktree some process still
uses, simulator devices whose runtime is gone (`xcrun simctl delete unavailable` deletes
their app data too), staged ChatGPT updates (App Management blocks deleting them from a
shell). A stale pack and drift are manager work: the message addresses the manager it goes to,
who settles it and asks the owner only what only they can decide.

## What it tells the chief of staff

The run posts into the chief of staff's Omnigent session (the session labelled
`omni.role=cos`, in any project), through the account's Omnigent CLI logins; a question about a repo's worktree
goes instead to that repo's project manager (`omni.role=pm`, `omni.repos` naming the repo), the
chief of staff when none is live, and so does a drift finding whose path is inside that repo's checkout. Only one server is asked: `server:` in
`~/.omnigent/config.yaml`, else `RUNNER_SERVER_URL`, else the CLI's default. Another logged-in server may be another
account's, so a configured server with no chief of staff, or one that can't be reached, fails the run (exit 1, one
log line naming the server) instead. It
posts in five cases, each question or finding once (`~/.local/state/agent-hygiene/asked`),
at most 30 per message:

- **Questions:** what it kept but may not be wanted (above). Nothing is deleted on a
  question; a yes means a manual removal, or a wider disposable rule in the script.
  `--hold-asks` prints them as `ASK:` lines instead.
- **Low disk:** when `/` still has under 20 GB free (`--floor-gb`), a warning at most once
  per 20 hours, and the run exits 2.
- **Stale pack:** the dotagents checkout pins another pack than its `origin/main` and was
  not fast-forwarded (Self-update).
- **Drift:** each finding of the checkout's `dotagents doctor`. A dotagents without the
  `doctor` command (exit 2) is itself a finding. A doctor that times out, exits other than
  0, 1 or 2, or exits 1 with no finding makes the run exit 1. A checkout a finding names is first
  brought up to date, then the doctor runs again: a finding that clears is logged, not told.
  With a clean tree it is installed from its committed lock (its `agent-sync` or `agent:sync`
  mise task with `--frozen` when its `mise.toml` has one, else `apm install --frozen`), after a
  fast-forward to origin's default branch (`git merge --ff-only`, not `git pull`, which follows the
  branch's own upstream) when it is on that branch with no local commits. A checkout that runs
  services (a process has it open or names it, or a systemd or launchd unit names its path, also
  through `%h` or `~`, or is a link to a file in it) is
  never pulled, only installed. A dirty, diverged or other-branch checkout is never touched, and
  its finding goes to the manager. Every install puts the lock back as it was, since a rewritten
  `generated_at` would dirty the tree and block the next pull, and puts back any committed agent
  file it changed (`.claude`, `.agents`, `.codex`, `AGENTS.md`, `CLAUDE.md`, `.apm`), since
  committed copies change only in a bump PR: that change is itself a finding, as is a failed
  install and a checkout with neither the task's `mise` nor `apm` on `PATH`.
- **Forwarders without their target:** a forwarder script in the home skill install
  (`~/.agents/skills`, `~/.claude/skills`; one that says it "only forwards to another skill's
  script") whose target skill is not installed beside it exits naming that skill. The run then
  runs `dotagents install --no-pull` once; a target still missing is a drift finding.

## Self-update

The script comes from the engineering pack that the account's dotagents checkout
(`~/.dotagents`) pins: `dotagents install` installs that pack in the checkout whenever its
`apm.lock.yaml` changes, and links `~/.local/bin/agent-hygiene` to this script there. A run
that is not a dry run fetches that checkout's `origin/main`. When it is ahead, the checkout
is on `main` and has no changes to tracked files (untracked files don't block it; git itself
refuses one that main would overwrite), it runs `git merge --ff-only origin/main`, then
`bin/dotagents install --no-pull`, then puts the committed `apm.lock.yaml` back (an install that
rewrote it would leave the tree dirty, and block the next fast-forward); a failed install logs `FAIL dotagents install`, the
run exits 1, and every later run installs again until one succeeds
(`~/.local/state/agent-hygiene/install-failed` holds the last failure). When the release moved the pin (`apm.lock.yaml`), the run starts over as the
newly installed script. A lock an earlier install left with only its `generated_at` changed is put back first. The pin
compared is the committed lock, never the file. When the pin differs from `origin/main`'s and no fast-forward
happened (tracked changes, another branch, diverged, a dry run), it says to pull and install
again, once per pair of locks.

## Schedule, log and exit codes

One Omnigent scheduled task per account, on that account's host, four times a day
(`FREQ=DAILY;BYHOUR=2,8,14,20;BYMINUTE=15`; stagger accounts that share a uv cache by 30
minutes), with `reuse_conversation` on so every run continues one conversation, and
`archive_when_done` on so a quiet round leaves the board (the next fire reopens it). The
prompt names this file and asks for a round (A round); the session calls
`sys_session_keep` with a one-line reason when the round needs the owner.

Each run logs to `~/.local/state/agent-hygiene/<UTC time>.log`, kept 30 days. Exit 0 is
done, 2 is free space under the floor (the chief of staff was told), 1 is a failed step,
process scan, install or message.
