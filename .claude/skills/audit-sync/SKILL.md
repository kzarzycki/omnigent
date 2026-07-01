---
name: audit-sync
description: Audited upstream sync for the omnigent fork. Detects the new upstream commits, audits that delta for supply-chain / security / breaking-change risk, and applies the sync-fork update ONLY when the audit passes (HOLD otherwise). Use when asked to safely sync/update omnigent to newest, or when driven on a schedule via /loop.
---

# Audited sync of the omnigent fork

One cycle: **detect the delta → audit it → apply only if clean.** This is the
`sync-fork` skill (the mechanical apply) gated behind the `update-delta-audit`
skill (the judgment). Run by the agent, not a headless script.

Repo: `~/dev/ext/omnigent/omnigent`. Branches/remotes are as in the
`sync-fork` skill (`upstream` = omnigent-ai, `mine` = patched branch).

## Procedure

1. **Detect the delta.**
   ```bash
   cd ~/dev/ext/omnigent/omnigent
   git fetch upstream -q
   BASE=$(git merge-base mine upstream/main)
   NEW=$(git rev-parse upstream/main)
   ```
   If `BASE == NEW`, already current — report "up to date" and stop. Nothing
   else runs.

2. **Audit `BASE..NEW`.** These are the upstream commits about to be replayed
   under your patches. Invoke the **`update-delta-audit`** skill over that
   range (`git log --stat BASE..NEW`, `git diff BASE..NEW`, `git show` on
   anything suspicious). Concentrate on:
   - new or bumped dependencies — `pyproject.toml`, `uv.lock`, `web/package.json`
   - new network calls, telemetry, or phone-home
   - auth / permission / policy / sandbox changes
   - anything executing at import or install time (build hooks, postinstall)
   - breaking behavioral changes to the server/CLI paths you rely on
   Produce findings with a severity each.

3. **Gate.**
   - **No high-severity finding → APPLY.** Run
     `.claude/skills/sync-fork/sync-fork.sh` (single invocation: rebase `mine`,
     rebuild the SPA if `web/` changed, reinstall, restart the server and wait
     for health, push). Then report the applied range and any notable new
     features the user gets.
   - **Any high-severity finding, or an inconclusive audit → HOLD.** Do NOT run
     sync-fork. Leave the working tree, install, and server untouched. Report
     the concern and the exact offending commits for manual review.

4. **Record.** End every cycle with a short report: delta range, verdict, and
   what was applied or why it was held.

## Notes

- HOLD is the safe default — never apply on an uncertain audit.
- The apply is a **single** `sync-fork.sh` call on purpose: the server
  stop→start happens inside one command and the script waits for health before
  returning, so the omnigent policy hook that gates the *next* command never
  catches the server mid-restart.
- Committed on `mine` (alongside `sync-fork`) so both replay across the very
  rebase they perform.
- Scheduling is separate — drive this procedure with `/loop` (see step 2 of the
  setup, handled on its own).
