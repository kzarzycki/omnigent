---
name: orchestrating-agents
description: Start, message, wait on, watch, list and close agent sessions through one backend (native subagents, Omnigent or herdr-link), with reports as files and questions as files. Use when starting a child agent such as a worker, verifier or lead, waiting on or collecting the agents you started, or choosing which backend runs them. Running or repairing an Omnigent install itself is operating-omnigent's.
---

# Orchestrating agents

## Backend

Agents are started, messaged, listed and closed through one backend, the first that holds:
a pin in the project's instructions (under the engineering loop, loop.md's line
`Orchestration backend: <name>`); the same line in the developer's personal instructions;
the session you run in, read from its environment (`OMNIGENT=1` is omnigent,
`HERDR_ENV=1` is herdr); else native. Then read that backend's file:
[backends/native.md](backends/native.md), the `omnigent-agents` skill (in the `omnigent`
package) or [backends/herdr-link.md](backends/herdr-link.md). Only that line names a backend: an
instruction about which CLI or model to use is not one, and never turns a child into a
separate process outside the backend, since nothing then lists, watches or closes it. This skill's
`scripts/omnigent_agent.py`, the Omnigent driver's former path, runs the `omnigent-agents`
skill's for one release line, after a deprecation line on stderr: call that one.

Each file gives the backend's operations: **start** a child with its brief, **message**
it, **wait** for its report, **watch** children nothing waits on, **list** them and
**close** them; Omnigent's also **approves** a child's pending approval prompt
(`approve`), on whatever authority the project's instructions give, and **broadcasts** one
message to many sessions at once (`broadcast`); a short read-only check or setup step on another host runs over SSH through `ssh --why` (never what the rule *Host work* lists), which turns the host's SSH
check into an owner card; a long-lived supervisor runs as a user
service that restarts it and survives a reboot (`supervise --registry <file> --install --log <file>`). A `scripts/` path is relative to this skill's folder; run it with
`python3` (standard library only).

## Rules

- **Reports are files.** An agent writes its report to a file and sends only the path to
  whoever started it, through the backend's message operation, then stops. It writes the
  report path once, at the end: whoever waits takes that file as the final report, so
  progress notes go to another file. Text left in
  its own session reaches nobody. A report names each agent's model, and returns only
  what the next step needs; the agent cleans up what it created first, and only that: a
  process it stops by the pid or process group it saved at launch, or by a pattern holding its
  own worktree path once each match's working directory is checked, never by a `pkill -f` or
  `killall` pattern other agents share, since that kills their runs too.
- **Questions are files.** A child never asks the owner, who is not watching it: it
  writes its question to `<report>.question`, replies with that path and stops. Whoever
  started it answers from the brief, the spec and the owner's stated intent, asks the owner
  only when none of them settles it, and sends the answer to the same child.
- **A wait wakes you, or you stay in the turn.** A harness wakes a session only for its own
  background tasks (Claude Code's `run_in_background` or Monitor, Codex's equivalent), a child's
  turn end through the backend, or a message, never for a process it does not track, and a cloud
  session not even for a native subagent it started. So a long
  build, test, gate or land runs as a harness-tracked background task, whose exit wakes you; or
  detached with a wrapper that tells you when it ends (the `omnigent-agents` skill's `wait-for
  --notify` with `LOOP_NOTIFY` set); or you wait on it in the same turn (`wait-for --log <log>
  --pid <pid file>`, called again on `still running`, or a cloud subagent's result). Never end a
  turn waiting on a bare pid, a log or, in a cloud session, a subagent, since nothing re-invokes
  you when it ends; Omnigent's supervisor nudges a lead that does.
- **Whoever starts a child collects it.** Keep a wait or a watch on each child you start,
  read its report before you report yourself, and close it once its report is in; never
  end your own work while one is still out, since its report then reaches nobody.
- **A taken result archives its session, at one point.** `run`, `wait`, the supervisor's relay of a
  child's `DONE` report or failure, a followed lead's `DONE` once a lead waiting on it (`--after`) is told, and `reap` all
  finish a session through the script's `finish_delivered`, so none has its own rule. A session stays
  open when it is kept (`--keep`, a `--pr` worker whose PR is open, an open question card, a pending prompt, background work);
  a lead that no lead waits on (one a followed lead started included) is archived by `reap <session>` from
  whoever started it, once it has its result; a named `reap` also finishes a session carrying `omni.keep`. An unarchive covers a follow-up.
  A run that ended failed, cancelled or errored counts as finished too; an `omnigent.keep` label holds
  nothing, since only an open card keeps a session; and archiving a parent finishes its children, bar one
  that is running, holds a card or is kept.
- **Host work.** Builds, tests, gates, lands and `git push` for another host run in a session there or in a cloud session, never over `ssh`.
