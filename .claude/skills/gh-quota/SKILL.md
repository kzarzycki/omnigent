---
name: gh-quota
description: Name who spends a GitHub account's API quota - a logging gh shim that records one line per call (time, host, session, cwd, parent command, arguments) without changing gh's output or exit code, a one-command report of calls per caller over the last N minutes, and the account's measured calls/min. Use when the account's rate limit empties early, to name the next burn, or to measure a fix.
metadata:
  requires: "gh python3"
---

# gh-quota

One GitHub account has one REST bucket (and one GraphQL bucket) shared by every machine and
session logged in as it, so a burn shows only as an empty bucket: nothing says who spent it.
The shim makes each machine keep its own count, and the report names the caller.

## Files

- [scripts/gh](scripts/gh), the logging shim. Installed ahead of the real gh on `PATH`, it
  appends one tab-separated line per call to its log and then `exec`s the real gh with the
  same arguments and streams, so gh's stdout, stderr and exit code never change. It makes no
  API call, so a call's `X-RateLimit-*` headers are not logged: read the bucket with `rate`
  below. Its own cost is a few forks (`date`, `hostname`, `ps`, `sed`), about 30-50 ms a call.
  - Line: UTC time, host, session (the `OMNIGENT_SESSION_ID` and
    `OMNIGENT_RUNNER_PRIMARY_SESSION_ID` ids, no other env value), cwd, parent command (and,
    when the parent is a shell, the command that ran it), gh arguments cut to 400 characters,
    with GitHub tokens, `Authorization:` headers, `secret`/`variable set` values and fields
    named for a token, secret, password, key or auth masked.
  - Log: `$GH_QUOTA_LOG`, else `${XDG_STATE_HOME:-~/.local/state}/gh-quota/calls.log`.
  - Real gh: `$GH_QUOTA_REAL`, else the first gh on `PATH` that is not a copy of the shim. A
    failure to write the log never stops the call.
- [scripts/gh_quota.py](scripts/gh_quota.py), the installer and reports:
  - `install [--dir DIR]` copies the shim to `DIR/gh` (default `~/.local/bin`), refuses to
    overwrite a real gh, and exits 1 naming the fix when `DIR` is not ahead of the real gh on
    `PATH`, since the shim then never runs. Run it again after an upgrade of this skill.
  - `report [--minutes N] [LOG ...] [--counts FILE ...]` prints calls per caller (host,
    session, parent command, each script path cut to its file name) over the last N minutes
    (default 60), with calls/min and each caller's top gh commands, PR numbers and SHAs
    folded and the method kept (`api POST repos/o/r/pulls/N/reviews`).
  - `counts [--minutes N] [LOG ...]` prints the same tally as tab-separated lines, small
    enough to post in a report or comment from a machine whose log no one else reads (a cloud
    VM); `report --counts FILE` adds such files into one ranking.
  - `rate [--minutes N] [--every S] [--bucket core|graphql]` samples `gh api rate_limit`,
    which costs no quota, every S seconds (default 60) for N minutes (default 10) and prints
    the account's spend in calls/min, across a reset too. A step between two windows whose count
    grew counts nothing (a reply from another bucket looks the same), so the rate errs low. Being the account's spend, it counts
    every machine, logged or not.

## Naming a burn

1. Install the shim on every machine that runs gh as the account, and in each cloud session
   (`gh_quota.py install`, with its folder ahead of the real gh). A cloud environment can do it
   in its setup script, so its sessions need no step: fetch the two files at a pinned commit,
   check their sums, keep them in `~/.local/share/gh-quota` and run `install` there, as this
   repo's `.claude/cloud-setup.sh` does.
2. Let it log while the burn runs, at least 10 minutes.
3. Run `gh_quota.py report --minutes 10` on each machine; from a cloud session, post
   `gh_quota.py counts --minutes 10` where the machine that ranks them can read it, and rank
   all of them with `report --counts`.
4. Compare the logged total with `gh_quota.py rate --minutes 10`: a rate well above the
   logged calls means a caller the shim does not see, such as a `curl` to `api.github.com`,
   a library's own HTTP client, or a machine without the shim.
