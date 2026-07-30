---
name: audit-sync
description: Audited upstream sync for the omnigent fork. Detects the new upstream commits, audits that delta for supply-chain / security / breaking-change risk, and applies the sync-fork update ONLY when the audit passes (HOLD otherwise). Use when asked to safely sync/update omnigent to newest, manually or on a schedule.
---

# Audited sync of the omnigent fork

One cycle: **detect the delta → audit it → apply only if clean.** This gates the
`sync-fork` skill (the mechanical apply) behind an audit of what upstream is
introducing. Run by an agent, not a dumb script — the audit needs judgement.

Repo: `~/dev/ext/omnigent/omnigent`. Branches/remotes are as in `sync-fork`
(`upstream` = omnigent-ai, `mine` = the patched branch).

## Procedure (what the agent does each run)

1. **Detect the delta.**
   ```bash
   cd ~/dev/ext/omnigent/omnigent
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
     It rebases `mine`, rebuilds the UI if `web/` changed, reinstalls, and
     **auto-skips the server restart when inside omnigent** (push is non-fatal).
   - **Any high-severity finding, or an inconclusive audit → HOLD:** do NOT run
     sync-fork. Leave the tree, install, and server untouched. Report the
     concern and the offending commits.

4. **Report** — a concise markdown summary of the delta and findings, ending
   with a final line `VERDICT: PASS` or `VERDICT: HOLD <reason>`.

## Running it on a schedule

`auto-sync.sh` (next to this file) is the launchd entrypoint. It runs **outside**
any omnigent session so it can restart the server the audit session runs under:

1. Detects the delta; exits early if none (never wakes an agent for nothing).
2. Drives a headless `omnigent run --harness claude -p …` session through the
   procedure above (the apply runs inside omnigent, so sync-fork skips its own
   restart).
3. If `mine` advanced (audit passed and applied), restarts the launchd server
   **here** to load the new backend, and posts a notification. Held / no-op →
   no restart.

The report and full log land in `~/.omnigent/logs/auto-sync/`.

### It triggers on the first real wake, not a clock time

`dev.zarz.omnigent-autosync` uses `StartInterval` (15m), **not**
`StartCalendarInterval`. On a laptop a fixed hour does not work:

- macOS runs scheduled work during **DarkWake**, which lasts 2-7 seconds here.
  Wifi has not associated yet, so the fetch dies — 12 of 18 runs on a 04:00
  schedule failed with `Could not resolve host` or a mid-transfer reset. A
  15-minute audit cannot complete in a 2-second wake either.
- `StartInterval` does not wake a sleeping Mac. launchd coalesces the missed
  firings and runs the job once on the next genuine wake.

`auto-sync.sh` then decides whether that wake deserves a run, in this order —
all before the per-run log is opened, since 96 ticks a day would otherwise
litter 96 files. Skips append one line to `ticks.log`:

| Guard | Behaviour |
|---|---|
| `.state/last-run` == today | exit. Written only when a run reaches a decision, so a no-network day retries instead of burning the day. |
| display off | exit. macOS holds a `Prevent sleep while display is on` assertion exactly while the user is present. |
| host-runner log touched < 10m ago | defer, so the post-sync server restart doesn't land under a live agent. Capped at 4h so a busy day can't starve the sync. |

**Is it working?** `tail ~/.omnigent/logs/auto-sync/ticks.log` — every tick that
did anything logs one line, and a skip names the guard that stopped it. Silence
all day means no tick fired at all (agent unloaded), not a quiet success.

## Gotchas

Each of these silently disabled the job at some point. They are load-bearing.

- **`launchctl bootout` needs `bootstrap`, not `kickstart`.** `kickstart` only
  restarts an already-loaded service; after a bootout the label is gone and
  kickstart fails forever. Both this script and `sync-fork.sh` check
  `launchctl print` and fall back to `bootstrap`.
- **`pipefail` + `grep -q` inverts the result.** `grep -q` exits on first match,
  SIGPIPEs the writer, and with `set -o pipefail` the *pipeline* reports
  failure — so a matching guard reads as "no match". Read into a variable and
  match with a herestring instead. This one made the display guard report
  "asleep" while the machine was in use.
- **`IODisplayWrangler` does not exist on Apple Silicon.** The usual
  display-power check via `ioreg -n IODisplayWrangler` silently returns the root
  node. Use the `pmset -g assertions` string above.
- **Don't key "busy" on a live process.** Orphaned runner processes outlive
  their session by many minutes; a `pgrep`-based check defers forever. Log mtime
  reflects actual work.
- **`stat -f %z` is ambiguous.** BSD `stat` reads `-f` as a format string, GNU
  coreutils reads it as "filesystem", and a homebrew coreutils on `PATH` decides
  which you get. Use `wc -c < file`.
- **A clean audit that failed to apply looks exactly like a HOLD** — both leave
  `mine` unchanged. Split on the report verdict and surface the session exit
  code, or a crashed apply reports as "held" for days.

## Notes

- **HOLD is the safe default** — never apply on an uncertain audit.
- The restart is the *only* self-terminating step; it is owned exclusively by
  `auto-sync.sh` (outside omnigent), never by the in-session apply.
- Committed on `mine` (with `sync-fork`) so both replay across the rebase they
  perform.
