---
name: audit-sync
description: Audited upstream sync for the omnigent fork. Detects the new upstream commits, audits that delta for supply-chain / security / breaking-change risk, and applies the sync-fork update ONLY when the audit passes (HOLD otherwise). Use when asked to safely sync/update omnigent to newest, manually or on a schedule.
---

# Audited sync of the omnigent fork

One cycle: **detect the delta → audit it → apply only if clean.** This gates the
`sync-fork` skill (the mechanical apply) behind an audit of what upstream is
introducing. Run by an agent, not a dumb script — the audit needs judgement.

Repo: the fork checkout the run starts in (`~/dev/ext/omnigent/omnigent` on the
sync machine). Branches/remotes are as in `sync-fork`
(`upstream` = omnigent-ai, `mine` = the patched branch).

## Procedure (what the agent does each run)

1. **Detect the delta.**
   ```bash
   git fetch upstream -q
   BASE=$(git merge-base mine upstream/main)
   NEW=$(git rev-parse upstream/main)
   ```
   If `BASE == NEW`, already current — report "up to date" and stop.

2. **Audit `BASE..NEW`** — the upstream commits about to be replayed under your
   patches (`git log --stat BASE..NEW`, `git diff BASE..NEW`, `git show` on
   anything suspicious). Use the `update-delta-audit` skill if available; the
   criteria are self-contained here so it works without it. Flag **HIGH**
   severity for any of:
   - new or widened network egress; telemetry / phone-home
   - risky new or bumped dependencies (`pyproject.toml`, `uv.lock`, `web/package.json`)
   - auth / permission / sandbox changes
   - code executing at import or install time (build/postinstall hooks)
   - breaking changes to the server/CLI paths this setup relies on

3. **Gate.**
   - **No high-severity finding → APPLY:** run `bash .claude/skills/sync-fork/sync-fork.sh`.
     It takes commits landed in `origin/mine`, rebases `mine` onto upstream and
     pushes. It deploys nothing: each machine's `omnigent-update` task does.
   - **Any high-severity finding, or an inconclusive audit → HOLD:** do NOT run
     sync-fork. Leave the tree and `mine` untouched. Report the
     concern and the offending commits.

4. **Report** — a concise markdown summary of the delta and findings, ending
   with a final line `VERDICT: PASS` or `VERDICT: HOLD <reason>`.

## Running it on a schedule

An Omnigent scheduled task on the always-on VPS host (`cc-experiments`) runs
this procedure daily, with the fork checkout as its workspace. Pushing works
from its sessions (git credentials via `gh auth setup-git`), and no restart is
needed here, so nothing has to run outside Omnigent.

Deploying is separate: every machine has its own scheduled task running
`omnigent-update`, which installs the pushed `origin/mine` head and restarts
the host or server only when no session was active in the last 10 minutes.

## Notes

- **HOLD is the safe default** — never apply on an uncertain audit.
- **A clean audit that failed to apply looks exactly like a HOLD** — both leave
  `mine` unchanged. Report the apply's exit status next to the verdict, or a
  crashed apply reads as "held" for days.
- Committed on `mine` (with `sync-fork`) so both replay across the rebase they
  perform.
