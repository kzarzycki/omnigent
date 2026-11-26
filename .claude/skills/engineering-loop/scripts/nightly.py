#!/usr/bin/env -S uv run -q --script
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""Nightly (engineering-loop): a red nightly opens one issue listing the merges that could have caused it, and
`moved push|schedule|tag` says whether a CI run has anything to run (R7, #2179, #2261, #2264).

    uv run scripts/nightly.py red --last-green <sha> --red <sha> --run <url> [--repo <owner/name>] [--dry-run]

A project's nightly calls it when it ends red (SKILL.md, Nightly), from a checkout of the default branch holding the
history between the two commits (a shallow checkout fetches it first, such as `fetch-depth: 0`): `--last-green` is
the newest default-branch commit a nightly passed on, `--red` the one this run failed on, `--run` the run's link.

It walks the default branch's first-parent history from the last green commit (excluded) to the red one (included),
oldest first, and finds each commit's PR: a squash or `land` commit's subject ends `(#<n>)`, a merge commit's starts
`Merge pull request #<n>`, and any other commit asks GitHub which merged PR made it; a commit no PR made (a direct
push) is listed as a commit. Each PR is shown with the issues its body links (a closing keyword, `Part of` or
`Implements` before `#<n>`, as the gatekeeper reads them) and each issue's epic (its parent issue).

Then, unless an open issue titled `Nightly red since <the last green's first 7 hex digits>` exists, it opens one with
that title, the run's link and the list; while that issue is open it comments on it instead, with the new run's link
and only the merges since the red commit its last post names, so each red night adds what is new and a person or a
lead bisects from the issue. Every post carries a hidden line `<!-- nightly-red last-green=<sha> red=<sha> -->`, which
the next run reads to find where the new merges start.

Every body is checked first against the local deny list, the file the environment variable `LOOP_DENY_LIST` names
(one term per line, `#` starts a comment, matched ignoring case at word boundaries): a line naming a term refuses the
post before any call to GitHub, since a public repo keeps an issue's first text in its edit history. A list the
variable names that can't be read refuses too. `--dry-run` prints the body it would post and posts nothing.

The repository is `--repo`, else `GITHUB_REPOSITORY` (set in GitHub Actions), else the `origin` remote's GitHub slug.
It needs only git and gh (REST calls through `gh api`, so a token that may read pull requests and write issues).

    python3 scripts/nightly.py moved push|schedule|tag

`moved` is the other verb: a project's CI calls it as its first step to learn whether the run has anything to run
(R7), and it writes `skip=true|false` to `GITHUB_OUTPUT`; the comment above the verb has the three modes, the
`land.queue` it reads and its environment. It needs only python3 and the job's token (`actions: read`).

On success it prints the issue's or the comment's URL. Exit codes: 0 posted (or printed, with `--dry-run`, which still reads GitHub to choose between issue and comment); 1 refused
by the deny list, or a gh call failed; 2 a usage error, a commit git can't find, or a last green commit that is not an
ancestor of the red one.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

# importing the gatekeeper skill's folding.py would cache its bytecode in the installed skill folder, an untracked change there
sys.dont_write_bytecode = True

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _installed import installed

sys.path.insert(0, str(installed("gatekeeper")))
from folding import fold

# the environment variable naming the local deny list, a file kept outside the repo (gatekeeper.py's DENY_LIST)
DENY_LIST = "LOOP_DENY_LIST"
TITLE = "Nightly red since {}"
SHORT = 7
MARKER = "<!-- nightly-red last-green={} red={} -->"
MARKED = re.compile(r"<!-- nightly-red last-green=([0-9a-f]{7,64}) red=([0-9a-f]{7,64}) -->")
# a squash commit's or land's subject: `<title> (#<n>)`; a merge commit's: `Merge pull request #<n> from ...`
SQUASHED = re.compile(r"\(#(\d+)\)\s*$")
MERGED = re.compile(r"^Merge pull request #(\d+)\b")
# the gatekeeper's links: GitHub's closing keywords, `Part of` and `Implements`, outside code and comments
LINKED = re.compile(
    r"\b(?:(close[sd]?|fix(?:e[sd])?|resolve[sd]?)|(part[ \t]+of)|(implements)):?[ \t]+#(\d+)\b",
    re.IGNORECASE,
)
QUOTED = re.compile(r"^[ \t>]*```.*?^[ \t>]*```|`[^`\n]*`|<!--.*?-->", re.DOTALL | re.MULTILINE)
REMOTE = re.compile(r"github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?/?$")
PAGE = 100


class Refused(Exception):
    """A post refused, or a call that failed: the message says why, and nothing was posted."""


class Usage(Exception):
    """A usage or git error: exit 2."""


def git(*args: str) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise Usage(f"git {' '.join(args)}: {result.stderr.strip() or f'exit {result.returncode}'}")
    return result.stdout


def gh(*args: str) -> Any:
    """One `gh api` call, its JSON answer parsed."""
    result = subprocess.run(["gh", "api", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise Refused(
            f"gh api {' '.join(args)}: {result.stderr.strip() or f'exit {result.returncode}'}"
        )
    return json.loads(result.stdout) if result.stdout.strip() else None


def denied(text: str) -> None:
    """Refuse `text` when a line names a term of the deny list `LOOP_DENY_LIST` names; without it, check nothing."""
    where = os.environ.get(DENY_LIST)
    if not where:
        return
    try:
        listed = Path(where).expanduser().read_text().splitlines()
    except OSError as exc:
        raise Refused(
            f"{DENY_LIST} names {where}, which can't be read ({exc.strerror}): nothing posted"
        ) from exc
    terms = [term for term in (line.split("#", 1)[0].strip() for line in listed) if term]
    for number, line in enumerate(text.splitlines(), 1):
        for term in terms:
            if re.search(rf"(?<!\w){re.escape(term)}(?!\w)", line, re.IGNORECASE):
                raise Refused(
                    f"refused to post: line {number} names `{term}` from the deny list ({DENY_LIST}); nothing posted"
                )


def repository(given: str | None) -> str:
    if given:
        return given
    if os.environ.get("GITHUB_REPOSITORY"):
        return os.environ["GITHUB_REPOSITORY"]
    found = REMOTE.search(git("remote", "get-url", "origin").strip())
    if not found:
        raise Usage(
            "no --repo, no GITHUB_REPOSITORY, and the origin remote is not a GitHub repository"
        )
    return found.group(1)


def commit(ref: str) -> str:
    try:
        return git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").strip()
    except Usage:
        raise Usage(
            f"no commit {ref} in this checkout (a shallow one fetches the history first)"
        ) from None


def ancestor(older: str, newer: str) -> bool:
    return (
        subprocess.run(["git", "merge-base", "--is-ancestor", older, newer], check=False).returncode
        == 0
    )


def commits(since: str, until: str) -> list[tuple[str, str, int]]:
    """The first-parent commits after `since` up to `until`, oldest first: (sha, subject, parent count)."""
    out = git("log", "--first-parent", "--reverse", "--format=%H%x00%P%x00%s", f"{since}..{until}")
    rows = []
    for line in out.splitlines():
        sha, parents, subject = line.split("\x00", 2)
        rows.append((sha, subject, len(parents.split())))
    return rows


def links(body: str) -> list[tuple[int, str]]:
    """The issues a PR body links, in order, each once with its kind; a closing keyword wins, then `Implements`."""
    rank = ("closes", "implements", "part of")
    found: dict[int, str] = {}
    for closes, part, implements, number in LINKED.findall(QUOTED.sub("", body or "")):
        kind = "closes" if closes else "implements" if implements else "part of"
        found[int(number)] = min(found.get(int(number), kind), kind, key=rank.index)
    return list(found.items())


class GitHub:
    """The REST reads and writes, each issue and PR read once."""

    def __init__(self, repo: str) -> None:
        self.repo = repo
        self.issues: dict[int, dict[str, Any]] = {}

    def issue(self, number: int) -> dict[str, Any]:
        if number not in self.issues:
            self.issues[number] = gh(f"repos/{self.repo}/issues/{number}")
        return self.issues[number]

    def epic(self, number: int) -> dict[str, Any] | None:
        if not self.issue(number).get("parent_issue_url"):
            return None
        parent = gh(f"repos/{self.repo}/issues/{number}/parent")
        self.issues.setdefault(parent["number"], parent)
        return parent

    def pull(self, number: int) -> dict[str, Any]:
        return gh(f"repos/{self.repo}/pulls/{number}")

    def made(self, sha: str) -> int | None:
        """The PR whose merge made commit `sha`, by GitHub's commit-to-PR lookup."""
        for pull in gh(f"repos/{self.repo}/commits/{sha}/pulls") or []:
            if pull.get("merge_commit_sha") == sha:
                return int(pull["number"])
        return None

    def open_issue(self, title: str) -> dict[str, Any] | None:
        page = 1
        while True:
            found = (
                gh(
                    "-X",
                    "GET",
                    f"repos/{self.repo}/issues",
                    "-f",
                    "state=open",
                    "-f",
                    f"per_page={PAGE}",
                    "-f",
                    f"page={page}",
                )
                or []
            )
            for issue in found:
                if issue.get("title") == title and "pull_request" not in issue:
                    return issue
            if len(found) < PAGE:
                return None
            page += 1

    def comments(self, number: int) -> list[dict[str, Any]]:
        page, every = 1, []
        while True:
            found = (
                gh(
                    "-X",
                    "GET",
                    f"repos/{self.repo}/issues/{number}/comments",
                    "-f",
                    f"per_page={PAGE}",
                    "-f",
                    f"page={page}",
                )
                or []
            )
            every += found
            if len(found) < PAGE:
                return every
            page += 1

    def post(self, path: str, payload: dict[str, str]) -> dict[str, Any]:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(payload, handle)
        try:
            return gh(f"repos/{self.repo}/{path}", "--input", handle.name)
        finally:
            os.unlink(handle.name)


def line(hub: GitHub, sha: str, subject: str, parents: int) -> str:
    """One commit's line: its PR with the issues it links and their epics, or the bare commit."""
    short = sha[:SHORT]
    found = SQUASHED.search(subject) or MERGED.match(subject)
    number = int(found.group(1)) if found else hub.made(sha)
    if number is None:
        return f"- `{short}` {subject} (no PR: a direct push)"
    pull = hub.pull(number)
    how = "merge" if parents > 1 else "squash"
    text = f"- #{number} {pull.get('title', '')} ({how} `{short}`)"
    linked = []
    for issue, kind in links(pull.get("body") or ""):
        if "pull_request" in hub.issue(issue):
            continue
        epic = hub.epic(issue)
        linked.append(f"{kind} #{issue}" + (f", epic #{epic['number']}" if epic else ""))
    return text + (": " + "; ".join(linked) if linked else ": links no issue")


def listing(hub: GitHub, since: str, until: str) -> list[str]:
    return [line(hub, sha, subject, parents) for sha, subject, parents in commits(since, until)]


def red(args: argparse.Namespace) -> int:
    green, failed = commit(args.last_green), commit(args.red)
    if not ancestor(green, failed):
        raise Usage(
            f"the last green {green[:SHORT]} is not an ancestor of the red {failed[:SHORT]}"
        )
    hub = GitHub(repository(args.repo))
    title = TITLE.format(green[:SHORT])
    existing = hub.open_issue(title)
    marker = MARKER.format(green, failed)
    if existing is None:
        lines = listing(hub, green, failed) or ["- none: the red commit is the last green one"]
        body = (
            fold(
                f"The nightly is red since `{green[:SHORT]}`, its last green commit; "
                + f"it failed on `{failed[:SHORT]}`: {args.run}",
                details="Merged on the default branch since the last green, oldest first; bisect from these:\n\n"
                + "\n".join(lines),
            )
            + f"\n{marker}"
        )
    else:
        posts = [existing.get("body") or ""] + [
            c.get("body") or "" for c in hub.comments(existing["number"])
        ]
        reds = [m.group(2) for post in posts for m in MARKED.finditer(post)]
        since = next((sha for sha in reversed(reds) if ancestor(sha, failed)), green)
        lines = listing(hub, since, failed) or [
            f"- none since `{since[:SHORT]}`, the red commit the last post names"
        ]
        body = (
            fold(
                f"Still red on `{failed[:SHORT]}`: {args.run}",
                details=f"Merged since `{since[:SHORT]}`, oldest first:\n\n" + "\n".join(lines),
            )
            + f"\n{marker}"
        )
    denied(body)
    if args.dry_run:
        print(body)
        return 0
    if existing is None:
        made = hub.post("issues", {"title": title, "body": body})
    else:
        made = hub.post(f"issues/{existing['number']}/comments", {"body": body})
    print(made.get("html_url", ""))
    return 0


# --- moved: does a run on the default branch have anything to run (R7, #2179, #2261) ---------------------------------
# `python3 scripts/nightly.py moved push|schedule|tag`, stdlib only, which a consumer's job
# runs with the job's token (`GITHUB_TOKEN`, `GITHUB_REPOSITORY`, `GITHUB_API_URL`, `GITHUB_SHA`, `GITHUB_WORKFLOW_REF`;
# `actions: read`), never local state or an authenticated gh. It writes `skip=true|false` to `GITHUB_OUTPUT` and, with
# the run that did the work (`Skipped: ... <link>`), to the step summary and the log:
#   push      skip when a successful run of the same workflow on the merge queue's batch branch tested a commit whose
#             tree is the tree of `GITHUB_SHA`. The queue is the checkout's `docs/agents/gatekeeper.md` `land.queue`,
#             read with the gatekeeper's own reader: `mergify` batches are draft PRs on `mergify/merge-queue/*` (a
#             `pull_request` run), `github` batches are `gh-readonly-queue/*` refs (a `merge_group` run). Unset or
#             unreadable, both are read: GitHub's queue is chosen by the base's rules, not by the policy, which an
#             Actions token can't read, and a batch run of either queue that tested this tree tested it.
#   schedule  skip when the newest successful scheduled run on the default branch (`GITHUB_REF_NAME`, else `main`)
#             ran `GITHUB_SHA` itself.
#   tag       skip when any successful scheduled run of the nightly workflow (`GITHUB_WORKFLOW_REF` names the tag
#             check's workflow there, so the nightly is named: `engineering-ci.yml`) ran `GITHUB_SHA`, the tagged commit
#             (#2264): one runs call filtered by `event=schedule`, `status=success` and `head_sha`, checked again here.
# Any forge or API failure reads as "nothing found": the run goes ahead.
BATCHES = {  # land.queue -> (the batch branches' prefix, the event of the run that tests them)
    "mergify": ("mergify/merge-queue/", "pull_request"),
    "github": ("gh-readonly-queue/", "merge_group"),
}
RUN_PAGES = 3
NIGHTLY = "engineering-ci.yml"
FORGE_ERRORS = (OSError, KeyError, ValueError)  # network and HTTP errors, a missing token, bad JSON

Api = Callable[[str], object]


def forge(path: str) -> object:
    request = urllib.request.Request(
        os.environ.get("GITHUB_API_URL", "https://api.github.com") + path,
        headers={
            "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def land_queue() -> tuple[str | None, str]:
    """`land.queue` of the checkout's policy through the gatekeeper's reader, and the one line saying what it means:
    None when it is unset or the reader can't run (no PyYAML, no policy file), which reads both queues' batches."""
    try:
        path = installed("gatekeeper") / "gatekeeper.py"
        spec = importlib.util.spec_from_file_location("gatekeeper_policy", path)
        module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        queue = (module.settings(module.project()[1]).get("land") or {}).get("queue")
    except (Exception, SystemExit) as error:  # noqa: BLE001 - a policy the reader refuses, or no PyYAML: read both
        reason = str(error).splitlines()[0] if str(error) else type(error).__name__
        return None, f"land.queue unreadable ({reason}): batch branches of both queues read"
    if queue in BATCHES:
        return queue, f"land.queue is {queue}: batch branches {BATCHES[queue][0]}*"
    return None, f"land.queue is {queue or 'unset'}: batch branches of both queues read"


def batch_kinds(queue: str | None) -> list[tuple[str, str]]:
    return [BATCHES[queue]] if queue in BATCHES else list(BATCHES.values())


def workflow_runs(api: Api, repo: str, workflow: str, query: str) -> list[dict]:
    found: list[dict] = []
    for page in range(1, RUN_PAGES + 1):
        items = api(  # type: ignore[index]
            f"/repos/{repo}/actions/workflows/{workflow}/runs?{query}&per_page={PAGE}&page={page}"
        )["workflow_runs"]
        found += items
        if len(items) < PAGE:
            break
    return found


def tree_of(api: Api, repo: str, sha: str) -> str:
    return api(f"/repos/{repo}/git/commits/{sha}")["tree"]["sha"]  # type: ignore[index]


def tested_by_batch(api: Api, repo: str, workflow: str, sha: str, queue: str | None) -> dict | None:
    """The successful batch run that tested the tree of `sha`, else None."""
    tree = tree_of(api, repo, sha)
    for prefix, event in batch_kinds(queue):
        for item in workflow_runs(api, repo, workflow, f"event={event}&status=success"):
            if not str(item.get("head_branch", "")).startswith(prefix):
                continue
            try:
                if tree_of(api, repo, item["head_sha"]) == tree:
                    return item
            except (
                FORGE_ERRORS
            ) as error:  # a batch commit the forge no longer holds tested nothing we can read
                print(
                    f"nightly: batch commit {item['head_sha']} unreadable ({error})",
                    file=sys.stderr,
                )
    return None


def last_green_is(api: Api, repo: str, workflow: str, branch: str, sha: str) -> dict | None:
    """The newest successful scheduled run on `branch`, when it ran `sha`, else None."""
    found = api(  # type: ignore[index]
        f"/repos/{repo}/actions/workflows/{workflow}/runs?event=schedule&branch={branch}&status=success&per_page=1"
    )["workflow_runs"]
    return found[0] if found and found[0]["head_sha"] == sha else None


def nightly_ran(api: Api, repo: str, workflow: str, sha: str) -> dict | None:
    """A successful scheduled run of `workflow` that ran `sha`, else None."""
    found = api(  # type: ignore[index]
        f"/repos/{repo}/actions/workflows/{workflow}/runs?event=schedule&status=success&head_sha={sha}&per_page=1"
    )["workflow_runs"]
    # the forge filters on head_sha; the check here keeps a filter it ignored from reading as a match
    return next((item for item in found if item["head_sha"] == sha), None)


LINES = {
    "push": (
        "Skipped: this tree was tested by the merge queue's batch run ",
        "Running: no batch run tested this tree.",
    ),
    "schedule": (
        "Skipped: the default branch's head is the commit of the last green scheduled run ",
        "Running: the default branch moved since the last green.",
    ),
    "tag": (
        "Skipped test and test-slow: the nightly passed on the tagged commit in ",
        "Running: no green nightly ran the tagged commit.",
    ),
}


def moved_report(mode: str, found: dict | None, note: str = "") -> None:
    skip = found is not None
    line = LINES[mode][0 if skip else 1]
    if skip:
        line += found["html_url"]  # type: ignore[index]
    if note:
        line += f" ({note})"
    print(line)
    for name, text in (
        ("GITHUB_OUTPUT", f"skip={'true' if skip else 'false'}"),
        ("GITHUB_STEP_SUMMARY", line),
    ):
        if os.environ.get(name):
            with open(os.environ[name], "a") as handle:
                handle.write(text + "\n")


def moved(args: argparse.Namespace, api: Api = forge) -> int:
    repo, sha = os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_SHA"]
    workflow = (
        os.environ.get("GITHUB_WORKFLOW_REF", "").split("@")[0].rsplit("/", 1)[-1]
        or "engineering-ci.yml"
    )
    queue, note = land_queue() if args.mode == "push" else (None, "")
    try:
        if args.mode == "push":
            found = tested_by_batch(api, repo, workflow, sha, queue)
        elif args.mode == "schedule":
            found = last_green_is(
                api, repo, workflow, os.environ.get("GITHUB_REF_NAME") or "main", sha
            )
        else:
            found = nightly_ran(api, repo, NIGHTLY, sha)
    except FORGE_ERRORS as error:
        print(f"nightly: forge read failed ({error}); running as usual", file=sys.stderr)
        found = None
    moved_report(args.mode, found, note)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Nightly: open or extend the issue of a red nightly (red); say whether a run has anything to run (moved)."
    )
    verbs = parser.add_subparsers(dest="verb", required=True)
    verb = verbs.add_parser(
        "red", help="list the merges since the last green commit on the nightly's issue"
    )
    verb.add_argument(
        "--last-green", required=True, help="the newest default-branch commit a nightly passed on"
    )
    verb.add_argument("--red", required=True, help="the commit this nightly failed on")
    verb.add_argument("--run", required=True, help="the nightly run's link")
    verb.add_argument(
        "--repo", help="owner/name; default GITHUB_REPOSITORY, else the origin remote"
    )
    verb.add_argument("--dry-run", action="store_true", help="print the body, post nothing")
    skipping = verbs.add_parser(
        "moved",
        help="skip=true|false to GITHUB_OUTPUT: has a push, scheduled or tag-check run anything to run",
    )
    skipping.add_argument("mode", choices=tuple(LINES))
    args = parser.parse_args(argv)
    try:
        return moved(args) if args.verb == "moved" else red(args)
    except Usage as exc:
        print(f"nightly: {exc}", file=sys.stderr)
        return 2
    except Refused as exc:
        print(f"nightly: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
