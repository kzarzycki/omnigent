#!/usr/bin/env python3
"""gh-quota: name who spends a GitHub account's API quota.

  gh_quota.py install [--dir DIR]          put the logging shim (scripts/gh) at DIR/gh, ahead of the real gh
  gh_quota.py report [--minutes N] [LOG ...] gh calls per caller (host, session, parent command) over the last N minutes
  gh_quota.py counts [--minutes N] [LOG ...] the same as tab-separated counts, to post from a host or cloud VM
                                             and read back with `report --counts FILE`
  gh_quota.py rate [--minutes N] [--every S] the account's spend in calls/min, from `gh api rate_limit` (costs no quota)

The shim writes one tab-separated line per gh call: UTC time, host, session, cwd, parent command, gh arguments.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Iterable, Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

SHIM = Path(__file__).resolve().with_name("gh")
MARK = "gh-quota's logging shim"
FIELDS = ("time", "host", "session", "cwd", "parent", "args")


def default_log() -> Path:
    if found := os.environ.get("GH_QUOTA_LOG"):
        return Path(found)
    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state) / "gh-quota" / "calls.log"


def is_shim(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return MARK.encode() in handle.read(120)
    except OSError:
        return False


def real_ghs(path: str) -> list[Path]:
    """Every gh on `path` that is not a copy of the shim, in PATH order."""
    found = []
    for folder in path.split(os.pathsep):
        candidate = Path(folder or ".") / "gh"
        if candidate.is_file() and os.access(candidate, os.X_OK) and not is_shim(candidate):
            found.append(candidate)
    return found


def install(folder: Path, path: str) -> int:
    """Copy the shim to folder/gh (a copy, so a pack upgrade that moves its folder never breaks gh). Refuses to
    replace a real gh, and says when folder is not ahead of the real gh on PATH, since the shim then never runs."""
    target = folder / "gh"
    if target.exists() and not is_shim(target):
        print(
            f"refused: {target} is a real gh, not the shim; install into a folder ahead of it",
            file=sys.stderr,
        )
        return 1
    folder.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(".gh.gh-quota")
    shutil.copyfile(SHIM, temporary)
    temporary.chmod(temporary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    temporary.replace(target)
    folders = [Path(entry or ".").resolve() for entry in path.split(os.pathsep)]
    reals = real_ghs(path)
    if not reals:
        print(f"installed {target}, but no real gh is on PATH: install gh", file=sys.stderr)
        return 1
    ahead = folder.resolve() in folders and folders.index(folder.resolve()) < folders.index(
        reals[0].parent.resolve()
    )
    if not ahead:
        print(
            f"installed {target}, but {folder} is not ahead of {reals[0]} on PATH, so the shim never runs: put it first",
            file=sys.stderr,
        )
        return 1
    print(f"installed {target} ahead of {reals[0]}; log: {default_log()}")
    return 0


def caller(parent: str) -> str:
    """The parent command, shortened: each path to its last part (`/x/y/approvals.py` -> `approvals.py`), so one
    script read from two checkouts counts once; long argument lists cut to 100 characters."""
    words = [
        word.rsplit("/", 1)[-1] if "/" in word and not word.startswith("-") else word
        for word in parent.split()
    ]
    return " ".join(words)[:100] or "-"


# gh api's flags that take a value, so the route is the first word that is neither a flag nor a flag's value
VALUED = {
    "-X",
    "--method",
    "-H",
    "--header",
    "-f",
    "--raw-field",
    "-F",
    "--field",
    "-q",
    "--jq",
    "-t",
    "--template",
    "--input",
    "-p",
    "--preview",
    "--hostname",
    "--cache",
}


def command(args: str) -> str:
    """The gh command, without its values: `api -X POST repos/o/r/pulls/12/reviews` -> `api POST
    repos/o/r/pulls/N/reviews`; any other command is its first two words (`pr view`)."""
    try:
        words = shlex.split(
            args
        )  # the shim quotes a word with a space: `-H 'Accept: x'` is one value
    except ValueError:
        words = args.split()
    if not words:
        return "-"
    if words[0] != "api":
        return " ".join(words[:2])
    method, route, skip = "", "", False
    for before, word in zip(words, words[1:]):
        if skip:
            skip = False
            if before in ("-X", "--method"):
                method = word.upper() + " "
            continue
        if word in VALUED:
            skip = True
        elif word.startswith("--method="):
            method = word.split("=", 1)[1].upper() + " "
        elif not word.startswith("-") and not route:
            route = word
    route = re.sub(r"\?.*", "", route)
    return "api " + method + re.sub(r"/[0-9a-f]{7,40}(?=/|$)|/\d+(?=/|$)", "/N", route)


def calls(logs: Iterable[Path], since: datetime) -> Iterator[dict[str, str]]:
    stamp = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    for log in logs:
        try:
            lines = log.read_text(errors="replace").splitlines()
        except OSError as err:
            print(f"{log}: not read: {err.strerror}", file=sys.stderr)
            continue
        for line in lines:
            parts = line.split("\t")
            if len(parts) != len(FIELDS) or parts[0] < stamp:
                continue
            yield dict(zip(FIELDS, parts, strict=True))


def tally(logs: Iterable[Path], since: datetime) -> collections.Counter[tuple[str, str, str, str]]:
    counted: collections.Counter[tuple[str, str, str, str]] = collections.Counter()
    for call in calls(logs, since):
        counted[(call["host"], call["session"], caller(call["parent"]), command(call["args"]))] += 1
    return counted


def read_counts(files: Iterable[Path]) -> collections.Counter[tuple[str, str, str, str]]:
    counted: collections.Counter[tuple[str, str, str, str]] = collections.Counter()
    for file in files:
        for line in file.read_text().splitlines():
            parts = line.split("\t")
            if len(parts) == 5 and parts[0].isdigit():
                counted[(parts[1], parts[2], parts[3], parts[4])] += int(parts[0])
    return counted


def report(
    counted: collections.Counter[tuple[str, str, str, str]], minutes: float, top: int
) -> str:
    total = sum(counted.values())
    if not total:
        return f"no gh calls logged in the last {minutes:g} min"
    by_caller: collections.Counter[tuple[str, str, str]] = collections.Counter()
    commands: dict[tuple[str, str, str], collections.Counter[str]] = collections.defaultdict(
        collections.Counter
    )
    for (host, session, parent, cmd), n in counted.items():
        by_caller[(host, session, parent)] += n
        commands[(host, session, parent)][cmd] += n
    lines = [
        f"{total} gh calls in the last {minutes:g} min ({total / minutes:.1f}/min), by caller:",
        "",
    ]
    lines.append(f"{'calls':>6} {'/min':>6}  host  session  parent command  [top gh commands]")
    for key, n in by_caller.most_common(top):
        host, session, parent = key
        top_cmds = ", ".join(f"{cmd} x{c}" for cmd, c in commands[key].most_common(3))
        lines.append(f"{n:>6} {n / minutes:>6.1f}  {host}  {session}  {parent}  [{top_cmds}]")
    if len(by_caller) > top:
        lines.append(f"  ... {len(by_caller) - top} more callers")
    return "\n".join(lines)


def rate_limit() -> dict[str, dict[str, int]]:
    """`gh api rate_limit`, which costs no quota. The real gh is run, not the shim, so a sample logs nothing."""
    reals = real_ghs(os.environ.get("PATH", ""))
    out = subprocess.run(
        [str(reals[0]) if reals else "gh", "api", "rate_limit"],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout
    return json.loads(out)["resources"]


def spent(samples: list[tuple[float, int, int]]) -> int:
    """Calls spent across (time, used, reset) samples: the growth of `used` within one window, and after a reset (a
    later reset time and a smaller count) its new count. Two samples of different windows whose count grew are not one
    bucket (a proxy may answer from another token's), so that step counts nothing rather than a whole bucket; a real
    reset whose new window already used more than the old one is undercounted the same way, which errs low."""
    total = 0
    for (_, used, reset), (_, now_used, now_reset) in zip(samples, samples[1:]):
        if now_reset == reset:
            total += max(now_used - used, 0)
        elif now_reset > reset and now_used < used:
            total += now_used
    return total


def rate(minutes: float, every: float, bucket: str) -> int:
    samples: list[tuple[float, int, int]] = []
    end = time.monotonic() + minutes * 60
    while True:
        try:
            found = rate_limit()[bucket]
        except (subprocess.SubprocessError, OSError, ValueError, KeyError) as err:
            print(f"rate_limit not read: {err}", file=sys.stderr, flush=True)
        else:
            samples.append((time.monotonic(), found["used"], found["reset"]))
            when = datetime.now(timezone.utc).strftime("%H:%M:%SZ")
            print(
                f"{when} {bucket} used {found['used']}/{found['limit']}, {found['remaining']} left",
                flush=True,
            )
        if time.monotonic() >= end:
            break
        time.sleep(min(every, max(end - time.monotonic(), 0)))
    if len(samples) < 2:
        print("too few rate_limit samples to measure a rate", file=sys.stderr)
        return 1
    span = (samples[-1][0] - samples[0][0]) / 60
    total = spent(samples)
    print(
        f"{bucket}: {total} calls in {span:.1f} min = {total / span:.1f} calls/min (~{total / span * 60:.0f}/h)"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    verbs = parser.add_subparsers(dest="verb", required=True)
    put = verbs.add_parser("install", help="put the logging shim ahead of the real gh")
    put.add_argument("--dir", type=Path, default=Path.home() / ".local" / "bin")
    for name in ("report", "counts"):
        verb = verbs.add_parser(
            name, help="gh calls per caller" if name == "report" else "the same as counts to post"
        )
        verb.add_argument("--minutes", type=float, default=60)
        verb.add_argument("logs", nargs="*", type=Path, help=f"default: {default_log()}")
        if name == "report":
            verb.add_argument(
                "--counts", nargs="*", type=Path, default=[], help="files `counts` wrote, added in"
            )
            verb.add_argument("--top", type=int, default=20)
    sample = verbs.add_parser("rate", help="the account's calls/min from gh api rate_limit")
    sample.add_argument("--minutes", type=float, default=10)
    sample.add_argument("--every", type=float, default=60)
    sample.add_argument("--bucket", default="core")
    args = parser.parse_args(argv)
    if args.verb == "install":
        return install(args.dir, os.environ.get("PATH", ""))
    if args.verb == "rate":
        return rate(args.minutes, args.every, args.bucket)
    since = datetime.now(timezone.utc) - timedelta(minutes=args.minutes)
    logs = args.logs or ([] if args.verb == "report" and args.counts else [default_log()])
    counted = tally(logs, since)
    if args.verb == "counts":
        for (host, session, parent, cmd), n in counted.most_common():
            print(f"{n}\t{host}\t{session}\t{parent}\t{cmd}")
        return 0
    counted.update(read_counts(args.counts))
    print(report(counted, args.minutes, args.top))
    return 0


if __name__ == "__main__":
    sys.exit(main())
