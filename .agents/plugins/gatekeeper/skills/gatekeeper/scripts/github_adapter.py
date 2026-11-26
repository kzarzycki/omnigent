"""The gatekeeper's GitHub adapter: the tracker, forge and evidence ports (gatekeeper.py, Ports) through `gh`, in the
checkout's repo. gatekeeper.py loads it beside itself by path (sibling) and hands it `Refused`, `public` and `denied`;
nothing else imports it.

Every call goes through gh(), which checks each posted body against the local deny list first (denied), and every read
GraphQL serves goes through fast(), which reads REST instead where GitHub refuses GraphQL with a 403, as a cloud
session's proxy does. Reads return GitHub's GraphQL node shapes (ISSUE, PULL), which the policy reads.
"""

from __future__ import annotations

import io
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.parse
import zipfile
from collections.abc import Callable, Sequence
from typing import Any, TypeVar


# gatekeeper.py sets these four once it has loaded this module (sibling): the refusal that exits 1, the masking of a
# home or temp path in a posted text, the deny list's refusal of a text naming a listed term, and OWN_CHECKS
class Refused(Exception):
    """A reason to stop with exit 1, printed as is: gatekeeper.py's own, once it loads this module."""


def public(text: str) -> str:
    """`text` as posted: gatekeeper.py's public(), once it loads this module."""
    return text


def denied(text: str) -> None:
    """Refuse to post `text` naming a deny-list term: gatekeeper.py's denied(), once it loads this module."""


# the collaborator permissions that can write; GitHub's `permission` field reads `maintain` as `write`
WRITE = {"admin", "maintain", "write"}


NOTE = "id url author { login } body createdAt lastEditedAt isMinimized viewerCanMinimize"


SPECCED = f"number state body createdAt lastEditedAt labels(first: 50) {{ nodes {{ name }} }} comments(last: 100) {{ totalCount nodes {{ {NOTE} }} }}"


ASSIGNEES = "assignees(first: 10) { nodes { login } }"


# who added `approved:spec` (approver) and who was assigned when (release); ponytail: the newest 100 such events
EVENTS = """timelineItems(last: 100, itemTypes: [LABELED_EVENT, ASSIGNED_EVENT]) { nodes { __typename
  ... on LabeledEvent { createdAt actor { __typename login } label { name } }
  ... on AssignedEvent { createdAt assignee { ... on User { login } ... on Bot { login } } } } }"""


# an issue with its author and its native parent (an epic), which spec_policy() reads, its assignees and events
ISSUE = f"{SPECCED} author {{ login }} parent {{ {SPECCED} }} {ASSIGNEES} {EVENTS}"


# a PR's review threads, which PULL reads and rest_threads() asks for alone where GitHub's REST has none
THREADS = "reviewThreads(first: 100) { totalCount nodes { isResolved comments(first: 50) { nodes { body } } } }"


PULL = f"""id number url title isCrossRepository author {{ __typename login }} {ASSIGNEES} state isDraft mergeable autoMergeRequest {{ enabledAt }} isInMergeQueue body baseRefName baseRefOid headRefName headRefOid baseRepository {{ nameWithOwner defaultBranchRef {{ name }} }} labels(first: 50) {{ nodes {{ name }} }} comments(last: 100) {{ totalCount nodes {{ {NOTE} }} }}
  files(first: 100) {{ totalCount nodes {{ path changeType additions deletions }} }} latestReviews(first: 100) {{ nodes {{ state author {{ login }} }} }}
  reviews(last: 100) {{ totalCount nodes {{ author {{ login }} state body submittedAt commit {{ oid }} }} }}
  {THREADS}
  authored: commits(first: 100) {{ totalCount nodes {{ commit {{ author {{ user {{ login }} }} committer {{ user {{ login }} }} }} }} }}
  timelineItems(last: 100, itemTypes: [LABELED_EVENT, READY_FOR_REVIEW_EVENT]) {{ nodes {{ __typename
    ... on LabeledEvent {{ createdAt label {{ name }} }} ... on ReadyForReviewEvent {{ createdAt }} }} }}
  mergeQueueEntry {{ state position headCommit {{ committedDate checkSuites {{ totalCount }} }} }}
  removals: timelineItems(last: 50, itemTypes: [REMOVED_FROM_MERGE_QUEUE_EVENT]) {{ nodes {{
    ... on RemovedFromMergeQueueEvent {{ reason createdAt beforeCommit {{ oid }} }} }} }}
  commits(last: 1) {{ nodes {{ commit {{ statusCheckRollup {{ contexts(first: 100) {{ totalCount nodes {{
    __typename ... on CheckRun {{ name status conclusion startedAt completedAt checkSuite {{ status workflowRun {{ workflow {{ name }} }} }} }}
    ... on StatusContext {{ context state createdAt targetUrl }} }} }} }} }} }} }}
  closingIssuesReferences(first: 50) {{ nodes {{ repository {{ nameWithOwner }} {ISSUE} }} }}"""


def merged_labelled(branch: str, name: str) -> list[int]:
    """The merged PRs into `branch` that carry the label `name`."""
    # ponytail: one page of 100 through GraphQL; page when a feature branch takes more such PRs than that
    found = fast(
        lambda: gh(
            "pr",
            "list",
            "--base",
            branch,
            "--state",
            "merged",
            "--label",
            name,
            "--limit",
            "100",
            "--json",
            "number",
            "--jq",
            ".[].number",
        ),
        # REST: every closed PR into the branch, the merged ones carrying the label
        lambda: "\n".join(
            str(pull["number"])
            for pull in rest_pages(f"pulls?state=closed&base={branch}&per_page=100")
            if pull.get("merged_at")
            and name in {label["name"] for label in pull.get("labels") or []}
        ),
    )
    return [int(number) for number in found.split()]


def open_pulls(limit: int) -> list[dict[str, Any]]:
    """The repo's open PRs, up to `limit`, each with its number, branch, base, whether it is a draft, body and closing references (each naming its
    issue's repo as `nameWithOwner`, as the PR query does), in one call."""
    found = fast(
        lambda: json.loads(
            gh(
                "pr",
                "list",
                "--state",
                "open",
                "--limit",
                str(limit),
                "--json",
                "number,headRefName,baseRefName,isDraft,body,closingIssuesReferences",
            )
        ),
        # REST has no closing references: closed() reads each body's lines, as it does for the PR checked
        lambda: [
            {
                "number": other["number"],
                "headRefName": other["head"]["ref"],
                "baseRefName": other["base"]["ref"],
                "isDraft": bool(other.get("draft")),
                "body": other.get("body") or "",
                "closingIssuesReferences": [],
            }
            for other in rest_pages("pulls?state=open&per_page=100")[:limit]
        ],
    )
    for other in found:
        nodes = other["closingIssuesReferences"]
        for issue in nodes:
            where = issue["repository"]
            issue["repository"] = {"nameWithOwner": f"{where['owner']['login']}/{where['name']}"}
        other["closingIssuesReferences"] = {"nodes": nodes}
    return found


def open_pull_files(limit: int) -> list[dict[str, Any]]:
    """Up to `limit` open PRs, each with its number, base, head commit and the paths it changes (`files`), in one call.
    `gh pr list` names a PR's first 100 files only, so a PR with 100 has all of its files re-read through REST's pages
    (GitHub's own cap there is 3000), and an overlap past the 100th file is never missed."""

    def every_file(number: int) -> list[str]:
        return [file["filename"] for file in pr_changes(number)]

    return fast(  # type: ignore[no-any-return]
        lambda: [
            {
                **pull,
                "files": every_file(pull["number"])
                if len(pull["files"]) >= 100
                else [file["path"] for file in pull["files"]],
            }
            for pull in json.loads(
                gh(
                    "pr",
                    "list",
                    "--state",
                    "open",
                    "--limit",
                    str(limit),
                    "--json",
                    "number,baseRefName,headRefOid,files",
                )
            )
        ],
        lambda: [
            {
                "number": other["number"],
                "baseRefName": other["base"]["ref"],
                "headRefOid": other["head"]["sha"],
                "files": every_file(other["number"]),
            }
            for other in rest_pages("pulls?state=open&per_page=100")[:limit]
        ],
    )


def texts(data: str) -> list[str]:
    """The text GitHub will publish from a post's `data`: every string a JSON payload holds, decoded (a review's body
    and its inline comments), else the data itself (a comment's body)."""
    try:
        found: list[Any] = [json.loads(data)]
    except ValueError:
        return [data]
    strings = []
    while found:
        value = found.pop()
        if isinstance(value, str):
            strings.append(value)
        elif isinstance(value, dict | list):
            found.extend(value.values() if isinstance(value, dict) else value)
    return strings


# The flag that makes `gh pr merge` go past the base's rules. It is the owner's, in the GitHub UI, never a helper's: agents
# act as the same Admin account, so this check is the only thing that keeps the bypass the owner's (#1722). Written in two
# parts so that no script of the pack holds it whole, which a test asserts.
BYPASS_FLAG = "-" * 2 + "ad" + "min"
# the one REST route that merges, and the only fields a helper may send it; the route may end in slashes
MERGE_ROUTE = re.compile(r"/pulls/\d+/merge/*(?:\?|$)")
MERGE_FIELDS = frozenset({"merge_method", "sha", "commit_title", "commit_message"})
# `gh api`'s field flags, in every spelling it accepts: `-f k=v`, `-fk=v`, `--field k=v`, `--field=k=v`, and the same
# for -F and --raw-field. A short flag may sit in a cluster (`-if k=v`), and these short flags take a value
FIELD_FLAGS = frozenset({"-f", "-F", "--field", "--raw-field"})
VALUE_SHORT = frozenset("HqtXpfF")


def api_fields(words: list[str]) -> tuple[list[str], bool]:
    """The field names a `gh api` call sends, and whether it reads its body from `--input` (either spelling)."""
    sent, body, i = [], False, 0
    while i < len(words):
        word, i = words[i], i + 1
        value: str | None = None
        if word == "--input" or word.startswith("--input="):
            body = True
        elif word in FIELD_FLAGS:
            value, i = (words[i] if i < len(words) else ""), i + 1
        elif word.startswith(("--field=", "--raw-field=")):
            value = word.split("=", 1)[1]
        elif word.startswith("-") and not word.startswith("--") and len(word) > 1:
            for at, char in enumerate(word[1:], 1):
                if char not in VALUE_SHORT:
                    continue
                rest = word[at + 1 :]
                if char in "fF":
                    value = rest.removeprefix("=") if rest else (words[i] if i < len(words) else "")
                if not rest:
                    i += 1
                break
        if value is not None:
            sent.append(value.split("=", 1)[0])
    return sent, body


def refuse_bypass(args: tuple[str, ...]) -> None:
    """Refuse a gh call that asks to merge past the base's rules: `gh pr merge` with the bypass flag, a call to the REST
    merge route (`PUT pulls/N/merge`) that sends any field but MERGE_FIELDS or its body as `--input`, or a GraphQL
    `mergePullRequest`, since any of them could carry a bypass the pack cannot read. Every gh call of the pack goes
    through gh(), so no helper of it merges that way."""
    words = [str(arg) for arg in args]
    if any(word == BYPASS_FLAG or word.startswith(f"{BYPASS_FLAG}=") for word in words):
        raise Refused(
            "refused: a merge past the base's rules is the owner's, in the GitHub UI; no helper of the pack passes the"
            " bypass flag"
        )
    if words[:1] != ["api"]:
        return
    sent, body = api_fields(words)
    if any(MERGE_ROUTE.search(word) for word in words):
        if body or (extra := sorted(set(sent) - MERGE_FIELDS)):
            raise Refused(
                "refused: a REST merge sends merge_method, sha, commit_title and commit_message only"
                + (f", not {', '.join(extra)}" if not body else ", not a body read from --input")
                + ": a merge past the base's rules is the owner's, in the GitHub UI"
            )
    elif "graphql" in words and any("mergepullrequest" in word.lower() for word in words):
        raise Refused(
            "refused: a GraphQL mergePullRequest is a merge the pack does not make: a merge past the base's rules is the"
            " owner's, in the GitHub UI"
        )


# the reads this run made (#2016): one `approve merge` or `record verdict` read the same PR, its timeline, reviews,
# files, comments and checks three or four times, about 40 REST calls; a repeat of the same `gh api` read within READ_TTL
# seconds is answered from here, and any other call (a write, or any gh command but a plain `gh api` read) empties it,
# so nothing is read back stale past this run's own write; each wait calls read_afresh() after its sleep, which
# empties it, so its next read is GitHub's however short the wait.
reads: dict[tuple[str, ...], tuple[float, str]] = {}
READ_TTL = 20.0
# each PR's REST file list, read once per run (pr_changes): every `gh pr list` or GraphQL call empties `reads`, and
# rest_pull, paths(), overlaps() and open_pull_files each ask for it. Kept by PR number for the run; only a write to
# GitHub (a body, a mutation, a non-GET), read_afresh() or forget() drops it, since only a push changes a PR's files
# and a wait calls read_afresh() after its sleep.
files_read: dict[int, list[dict[str, Any]]] = {}
# the plain reads this round that GitHub refused (a 403, an integration's refusal) or rate-limited (#2101): asked again
# inside the round, such a read gets the same error without a call, since a refusal or an empty bucket does not clear
# in seconds. One land round asked a refused `api user` four times. Only read_afresh(), which a wait calls after its
# sleep, or forget() drops them; a network failure is not kept, so the next ask still tries it.
failed: dict[tuple[str, ...], subprocess.CalledProcessError] = {}
WRITE_FLAGS = {"-f", "-F", "--field", "--raw-field", "--input"}
# a rate limit as gh prints it: primary (`API rate limit exceeded`), secondary, or abuse detection
RATE_LIMIT = re.compile(r"rate limit|abuse detection", re.IGNORECASE)


def plain_read(args: Sequence[str]) -> bool:
    """Whether `gh *args` is a `gh api` GET that may be answered from `reads`: no method but GET, no field or body
    (which make gh POST), no `-i` (rest_if_changed's ETag read, which wants the headers), and not `rate_limit`."""
    if args[:1] != ("api",) or "-i" in args or any("rate_limit" in arg for arg in args):
        return False
    if any(
        arg in WRITE_FLAGS or arg.startswith(("--field=", "--raw-field=", "--input="))
        for arg in args
    ):
        return False
    for flag, value in zip(args, args[1:]):
        if flag in ("-X", "--method") and value.upper() != "GET":
            return False
    return not any(arg.startswith("--method=") and arg[9:].upper() != "GET" for arg in args)


def read_afresh() -> None:
    """After a wait that reads again to see someone else's change (a push GitHub shows late, a PR that turns landable):
    empty `reads`, so the next read is GitHub's however short the wait was; a wait never answers itself. The wait sleeps
    on its own clock, which a test may fake."""
    reads.clear()
    files_read.clear()
    failed.clear()


def writes(args: Sequence[str], data: str | None) -> bool:
    """Whether `gh *args` may change a PR: a body, a mutation, a `gh api` that isn't a plain GET (a GraphQL query's
    `-f query=` is no write), or a `gh pr` command that edits, merges, comments, reviews, closes or readies."""
    text = " ".join(args).lower()
    if data is not None or "mutation" in text:
        return True
    if args[:1] == ("pr",):
        return args[1:2] not in (("list",), ("view",), ("status",), ("checks",), ("diff",))
    return (
        args[:1] == ("api",)
        and "graphql" not in text
        and "rate_limit" not in text
        and not plain_read(args)
    )


def gh(*args: str, data: str | None = None) -> str:
    key = tuple(args)
    # the quota guard's free read: neither kept nor a reason to forget
    if key[:2] == ("api", "rate_limit"):
        return _gh(*args, data=data)
    if data is None and plain_read(key):
        if (seen := reads.get(key)) and time.monotonic() - seen[0] < READ_TTL:
            return seen[1]
        if miss := failed.get(key):
            raise subprocess.CalledProcessError(miss.returncode, miss.cmd, miss.output, miss.stderr)
        try:
            out = _gh(*args)
        except subprocess.CalledProcessError as exc:
            if refused(exc.stderr) or throttled(exc.stderr):
                failed[key] = exc
            raise
        reads[key] = (time.monotonic(), out)
        return out
    reads.clear()  # a write, or a call this cache doesn't read: nothing read before it is trusted after it
    if writes(key, data):
        files_read.clear()
    return _gh(*args, data=data)


def _gh(*args: str, data: str | None = None) -> str:
    refuse_bypass(args)
    if data is not None:  # every comment and review approvals.py posts goes as `data`
        for text in texts(data):
            denied(text)
    backoff = 60
    while True:
        guard(args)
        try:
            return subprocess.run(
                ["gh", *args], input=data, check=True, capture_output=True, text=True
            ).stdout
        except subprocess.CalledProcessError as exc:
            if (
                pause := rate_limited(str(exc.stderr), backoff, args[:2] == ("api", "graphql"))
            ) is None:
                raise
            # jitter, so lands that waited on one reset do not all resume in the same second
            pause = jittered(pause)
            until = time.time() + pause
            if wait_until is None or time.monotonic() + pause > wait_until:
                exc.stderr = f"{exc.stderr}\nGitHub rate limit, resets at {time.strftime('%H:%M UTC', time.gmtime(until))}"
                raise
            said = (
                f"GitHub rate limit, waiting until {time.strftime('%H:%M UTC', time.gmtime(until))}"
            )
            # in slices that each print, so wait-for's stall check (a log silent for 300 s) never kills the wait
            for left in range(pause, 0, -SLICE):
                print(said, file=sys.stderr, flush=True)
                time.sleep(min(left, SLICE))
            backoff = min(backoff * 2, 600)


# land --wait sets this (time.monotonic) to the end of its ceiling: a rate limit then waits, else gh() fails with the reset time
wait_until: float | None = None
JITTER = 180  # most seconds of random delay added to a rate-limit wait: every waiter on one reset resumes over 3 min, not in its first second
SLICE = 60  # seconds between the lines a rate-limit wait prints

# answers a run keeps (#1771): (bucket, key) -> (time.monotonic when read, answer); writer, required_checks and a
# settled head's check runs (rest_runs). KEPT bounds their age, so a wait of hours still sees a rule, a permission or a
# rerun change within it; forget() drops them all, as a test does between cases
kept: dict[tuple[str, str], tuple[float, Any]] = {}
KEPT = 900
# the quota guard (guard): while land --wait runs (wait_until set), each gh call first reads the bucket it spends from
# `gh api rate_limit` (free; at most once per QUOTA_EVERY seconds), and one under QUOTA_FLOOR calls left waits for its
# reset inside the ceiling, else stops naming it; quota_left holds the last reading for poll_seconds
QUOTA_EVERY, QUOTA_FLOOR = 60, 50
# bucket -> (time.monotonic when read, remaining, limit, reset epoch)
quota_left: dict[str, tuple[float, int, int, int]] = {}


def forget() -> None:
    """Drop every answer this run kept: viewer's login, `kept`, `reads`, `files_read`, `failed` and the guard's last quota readings; and end any wait
    (wait_until), so no guard runs."""
    global wait_until
    wait_until = None
    viewer_login.clear()
    kept.clear()
    etags.clear()
    quota_left.clear()
    reads.clear()
    files_read.clear()
    failed.clear()


def bucket_of(args: Sequence[str]) -> str:
    """The rate-limit bucket a gh call spends: GraphQL's for `gh api graphql` and for any other gh command (gh pr,
    gh issue), which GitHub serves through GraphQL; REST's (`core`) for every other `gh api`."""
    return "core" if args[:1] == ("api",) and args[1:2] != ("graphql",) else "graphql"


def quota(bucket: str) -> tuple[int, int, int] | None:
    """(remaining, limit, reset epoch) of `bucket` from `gh api rate_limit`, which costs no quota, read at most once per
    QUOTA_EVERY seconds; None when it can't be read, which the guard and the poll take as no news."""
    if (seen := quota_left.get(bucket)) and time.monotonic() - seen[0] < QUOTA_EVERY:
        return seen[1:]
    try:
        remaining, limit, reset = map(
            int,
            gh(
                "api",
                "rate_limit",
                "--jq",
                f'.resources.{bucket} | "\\(.remaining) \\(.limit) \\(.reset)"',
            ).split(),
        )
    except (subprocess.CalledProcessError, ValueError):
        return None
    quota_left[bucket] = (time.monotonic(), remaining, limit, reset)
    return remaining, limit, reset


def guard(args: Sequence[str]) -> None:
    """While land --wait runs (wait_until set), a gh call whose bucket has under QUOTA_FLOOR calls left first waits for
    the bucket's reset, in SLICE steps that each print, when the reset falls inside the wait's ceiling, and otherwise
    stops with an error naming the reset, rather than spend the last calls and be refused mid-land. Outside a wait it
    does nothing, so a one-shot command is never held; the rate_limit read itself is never guarded."""
    if wait_until is None or tuple(args[:2]) == ("api", "rate_limit"):
        return
    bucket = bucket_of(tuple(args))
    found = quota(bucket)
    if found is None or found[0] >= QUOTA_FLOOR:
        return
    remaining, _, reset = found
    pause = jittered(max(reset - int(time.time()), 0) + 1)
    at = time.strftime("%H:%M UTC", time.gmtime(time.time() + pause))
    if time.monotonic() + pause > wait_until:
        raise subprocess.CalledProcessError(
            1,
            ["gh", *args],
            "",
            f"GitHub {bucket} quota nearly spent ({remaining} calls left), resets at {at}: land --wait stops"
            " before its ceiling would pass; land again after the reset",
        )
    said = f"GitHub {bucket} quota nearly spent ({remaining} calls left), waiting until {at}"
    for left in range(pause, 0, -SLICE):
        print(said, file=sys.stderr, flush=True)
        time.sleep(min(left, SLICE))
    quota_left.pop(bucket, None)  # the reset passed: read the bucket afresh


def jittered(pause: int) -> int:
    """`pause` plus up to JITTER random seconds, so the lands that waited on one reset resume over minutes, not in its
    first second; within a wait (wait_until set) never past the ceiling's room, so the jitter alone never stops a land
    whose reset falls inside it."""
    room = JITTER if wait_until is None else min(JITTER, int(wait_until - time.monotonic()) - pause)
    return pause + random.randint(0, max(room, 0))


def rate_limited(stderr: str, backoff: int, graphql: bool = False) -> int | None:
    """Seconds to wait when `stderr` is a GitHub rate-limit refusal, else None. A secondary limit waits for the
    `Retry-After` the error shows, else `backoff` (60 s, doubling). A primary one (`API rate limit exceeded`, or
    GitHub's `already exceeded for user ID n`) reads its bucket from `gh api rate_limit`, which costs no quota: an
    empty bucket waits to its reset, one with calls left was refused for the per-minute limit and waits `backoff`.
    The bucket is GraphQL's for `gh api graphql` and for any gh command whose error says `GraphQL:`, else REST's.
    ponytail: gh's error text shows no headers, so Retry-After is read only where it appears in the message; and a
    refusal may carry no HTTP status (a GraphQL one is HTTP 200), so the message alone is read."""
    graphql = graphql or "GraphQL:" in stderr
    if (
        re.search(r"secondary rate limit|abuse detection", stderr, re.IGNORECASE)
        or "HTTP 429" in stderr
    ):
        found = re.search(r"retry[- ]after:? *(\d+)", stderr, re.IGNORECASE)
        return int(found[1]) if found else backoff
    if not re.search(r"API rate limit (already )?exceeded", stderr):
        return None
    try:
        remaining, reset = map(
            int,
            subprocess.run(
                [
                    "gh",
                    "api",
                    "rate_limit",
                    "--jq",
                    f'.resources.{"graphql" if graphql else "core"} | "\\(.remaining) \\(.reset)"',
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.split(),
        )
    except (subprocess.CalledProcessError, ValueError):
        return backoff
    return backoff if remaining else max(reset - int(time.time()), 0) + 1


def _graphql(query: str, **fields: object) -> Any:
    args = [arg for key, value in fields.items() for arg in ("-F", f"{key}={value}")]
    # gh fills {owner} and {repo} from the current checkout
    out = gh(
        "api", "graphql", "-f", f"query={query}", "-F", "owner={owner}", "-F", "name={repo}", *args
    )
    return json.loads(out)["data"]["repository"]


def _repository(fields: str) -> str:
    return f"query($owner: String!, $name: String!, $n: Int!) {{ repository(owner: $owner, name: $name) {{ {fields} }} }}"


# how GitHub, or a cloud session's proxy in front of it, refuses GraphQL: `gh` ends its error with a 403, or, for a job
# token a private organization repository withholds fields from, prints GraphQL's FORBIDDEN message. `gh pr merge`
# names no `HTTP 403` where the proxy refuses it: it prints `non-200 OK status code: 403 Forbidden body: ...` (#1917)
REFUSALS = ("HTTP 403", "Resource not accessible by integration", "status code: 403 Forbidden")


def refused(stderr: str | None) -> bool:
    """Whether `gh`'s stderr says GitHub refused this token or path, as opposed to failing for another reason."""
    return any(text in (stderr or "") for text in REFUSALS)


def throttled(stderr: str | None) -> bool:
    """Whether `gh`'s stderr says GitHub rate-limited the call, which no refusal is, though both answer HTTP 403."""
    return bool(RATE_LIMIT.search(stderr or ""))


# set once GraphQL was refused, so the rest of this run reads REST without asking GraphQL again; GATEKEEPER_REST_ONLY=1
# sets it from the start, so a check can force the REST path on a token GraphQL would answer
_rest_only = os.environ.get("GATEKEEPER_REST_ONLY") == "1"


Answer = TypeVar("Answer")


def fast(graphql: Callable[[], Answer], rest: Callable[[], Answer]) -> Answer:
    """GraphQL's answer, the fast path, or REST's where GitHub refuses GraphQL with a 403, as a cloud session's proxy
    does. REST reads the same fields call by call, in the same shape. After one refusal the run reads REST alone, so a
    run there makes one refused GraphQL call at most. Any other error raises."""
    global _rest_only
    if not _rest_only:
        try:
            return graphql()
        except subprocess.CalledProcessError as exc:
            if not refused(exc.stderr):
                raise
            _rest_only = True
    return rest()


def rest(path: str) -> Any:
    return json.loads(gh("api", path))


# rest_if_changed's last answer for each path: (its ETag, the parsed body)
etags: dict[str, tuple[str, Any]] = {}
# a 304 as gh reports it: the status line `gh api -i` prints, or its error `gh: HTTP 304` where it exits 1 on one
NOT_MODIFIED = re.compile(r"\bHTTP(?:/[\d.]+)? 304\b")


def rest_if_changed(path: str) -> tuple[bool, Any]:
    """REST `repos/{owner}/{repo}/<path>` read with the ETag of its last read (`If-None-Match`): (False, the kept answer)
    on GitHub's 304, which costs no quota, else (True, the answer), keeping its ETag (#1915). The first read of a path
    is a plain one."""
    known = etags.get(path)
    args = [
        "api",
        "-i",
        *(["-H", f"If-None-Match: {known[0]}"] if known else []),
        f"repos/{{owner}}/{{repo}}/{path}",
    ]
    try:
        out = gh(*args)
    except subprocess.CalledProcessError as exc:
        if known and NOT_MODIFIED.search(f"{exc.stdout or ''}\n{exc.stderr or ''}"):
            return False, known[1]
        raise
    head, _, body = out.replace("\r\n", "\n").partition("\n\n")
    if known and NOT_MODIFIED.search(head.split("\n", 1)[0]):
        return False, known[1]
    found = json.loads(body or "{}")
    if tag := re.search(r"(?im)^etag:[ \t]*(\S[^\n]*?)[ \t]*$", head):
        etags[path] = (tag[1], found)
    return True, found


def rest_pages(path: str, items: str = ".[]") -> list[Any]:
    """Every item of a paged REST list, `items` picking them out of each page."""
    out = gh("api", "--paginate", f"repos/{{owner}}/{{repo}}/{path}", "--jq", items)
    return [json.loads(line) for line in out.splitlines()]


def actor(user: dict[str, Any] | None) -> dict[str, Any] | None:
    """A REST user as GraphQL names it: its login without the `[bot]` REST adds, and its type."""
    if not user:
        return None
    return {"login": user["login"].removesuffix("[bot]"), "__typename": user.get("type") or "User"}


def rest_notes(number: int) -> dict[str, Any]:
    """An issue's or PR's comments from REST, every page, as GraphQL's `comments` reads them. REST has no edit time,
    so a note has no `lastEditedAt` (versions() then reads its text alone), only `updatedAt`, which every edit moves;
    nor can this account's right to minimize be read, so approve() minimizes nothing it read here."""
    nodes = [
        {
            "id": found["node_id"],
            "author": actor(found.get("user")),
            "body": found.get("body") or "",
            "createdAt": found["created_at"],
            "updatedAt": found["updated_at"],
            "isMinimized": bool(found.get("minimized")),
            "viewerCanMinimize": False,
        }
        for found in rest_pages(f"issues/{number}/comments?per_page=100")
    ]
    return {"totalCount": len(nodes), "nodes": nodes}


# REST timeline events as GraphQL's timelineItems types them
TIMELINE = {
    "labeled": "LabeledEvent",
    "assigned": "AssignedEvent",
    "ready_for_review": "ReadyForReviewEvent",
}


def rest_timeline(number: int, events: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """An issue's or PR's label, assign and ready events from REST's timeline, every page (`events`, once read)."""
    nodes = []
    if events is None:
        events = rest_pages(f"issues/{number}/timeline?per_page=100")
    for event in events:
        if (kind := TIMELINE.get(event.get("event"))) is None:
            continue
        found: dict[str, Any] = {"__typename": kind, "createdAt": event.get("created_at")}
        if kind == "LabeledEvent":
            found |= {"actor": actor(event.get("actor")), "label": event.get("label")}
        if kind == "AssignedEvent":
            found["assignee"] = actor(event.get("assignee"))
        nodes.append(found)
    return {"nodes": nodes}


def rest_specced(found: dict[str, Any]) -> dict[str, Any]:
    """The SPECCED fields of a REST issue: its body and labels, and its comments read too. No `lastEditedAt`: REST has
    no edit time, so the spec's text names its version (versions)."""
    return {
        "number": found["number"],
        "state": found["state"].upper(),
        "body": found.get("body") or "",
        "createdAt": found["created_at"],
        "updatedAt": found["updated_at"],
        "labels": {"nodes": [{"name": label["name"]} for label in found.get("labels") or []]},
        "comments": rest_notes(found["number"]),
    }


def rest_issue(number: int) -> dict[str, Any] | None:
    """Issue #number as ISSUE reads it, from REST; None for a pull request, as GraphQL's `... on Issue` gives."""
    found = rest(f"repos/{{owner}}/{{repo}}/issues/{number}")
    if "pull_request" in found:
        return None
    try:
        parent = rest_specced(rest(f"repos/{{owner}}/{{repo}}/issues/{number}/parent"))
    except subprocess.CalledProcessError as exc:
        if "HTTP 404" not in (exc.stderr or ""):
            raise
        parent = None
    return rest_specced(found) | {
        "author": actor(found.get("user")),
        "parent": parent,
        "assignees": {"nodes": [actor(user) for user in found.get("assignees") or []]},
        "timelineItems": rest_timeline(number),
    }


# REST's file status as GraphQL's changeType
CHANGE_TYPES = {"added": "ADDED", "removed": "DELETED", "renamed": "RENAMED", "copied": "COPIED"}


def rest_authored(number: int, total: int) -> dict[str, Any]:
    """A PR's commits as `authored` reads them, from REST: each commit's author and committer account. A
    `Co-Authored-By:` trailer is not read: it is text the committer wrote, not another account's push, so exempt()
    ignores it (GraphQL's `author` is the commit's own author likewise, never a trailer's)."""
    nodes = []
    for found in rest_pages(f"pulls/{number}/commits?per_page=100"):
        committer = actor(found.get("committer"))
        # GitHub's web-flow commits for a bot or the API: GraphQL gives that committer no account
        if committer and committer["login"] == "web-flow":
            committer = None
        nodes.append(
            {
                "commit": {
                    "author": {"user": actor(found.get("author"))},
                    "committer": {"user": committer},
                }
            }
        )
    return {"totalCount": total, "nodes": nodes}


def rest_latest(reviews: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each reviewer's newest review that approves, requests changes or was dismissed: a comment-only review
    leaves a reviewer's request for changes standing, as GitHub's review decision reads it."""
    latest: dict[str, dict[str, Any]] = {}
    for review in reviews:
        if review["state"] in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            latest[(review.get("author") or {}).get("login") or ""] = review
    return list(latest.values())


def rest_checks(head: str) -> dict[str, Any]:
    """The head's checks as `statusCheckRollup` reads them, from REST: every check run (each rerun too), named by its
    workflow, and each status context's newest state. The check runs come from rest_runs, kept for a head whose runs
    all finished (#1771); the status contexts are read each time, since anyone can post one at any moment."""
    nodes = list(rest_runs(head))
    combined = rest(f"repos/{{owner}}/{{repo}}/commits/{head}/status?per_page=100")
    for status in combined.get("statuses") or []:
        nodes.append(
            {
                "__typename": "StatusContext",
                "context": status["context"],
                "state": status["state"].upper(),
                "createdAt": status.get("created_at"),
                "targetUrl": status.get("target_url"),
            }
        )
    total = len(nodes) - len(combined.get("statuses") or []) + combined.get("total_count", 0)
    return {"contexts": {"totalCount": total, "nodes": nodes}}


def rest_runs(head: str) -> list[dict[str, Any]]:
    """The head's check runs as rest_checks reads them: three paged reads, kept (`kept`) once they are settled
    (read_runs), since a finished head's runs change only by a rerun, which the age bound (KEPT) still sees. An
    unsettled head (a run queued or in progress, or none yet) is read again each time; a new head is a new key."""
    runs: list[dict[str, Any]] | None = fresh(("runs", head))
    if runs is None:
        runs, settled = read_runs(head)
        if settled:
            kept[("runs", head)] = (time.monotonic(), runs)
    return [dict(run) for run in runs]


def read_runs(head: str) -> tuple[list[dict[str, Any]], bool]:
    """The head's check runs, and whether they are settled: at least one, and every check run and workflow run on
    the head finished, so a workflow queued before its first job exists keeps the head unsettled. Suites are not
    asked, since GitHub leaves an app's suite with no runs queued for good."""
    runs = rest_pages(f"commits/{head}/check-runs?filter=all&per_page=100", ".check_runs[]")
    suites = {
        suite["id"]: suite
        for suite in rest_pages(f"commits/{head}/check-suites?per_page=100", ".check_suites[]")
    }
    flows_seen = rest_pages(f"actions/runs?head_sha={head}&per_page=100", ".workflow_runs[]")
    flows = {run["check_suite_id"]: run["name"] for run in flows_seen}
    nodes: list[dict[str, Any]] = []
    for run in runs:
        suite = (run.get("check_suite") or {}).get("id")
        flow = flows.get(suite)
        nodes.append(
            {
                "__typename": "CheckRun",
                "name": run["name"],
                "status": (run.get("status") or "").upper(),
                "conclusion": (run.get("conclusion") or "").upper() or None,
                "startedAt": run.get("started_at"),
                "completedAt": run.get("completed_at"),
                "checkSuite": {
                    "status": ((suites.get(suite) or {}).get("status") or "").upper(),
                    "workflowRun": {"workflow": {"name": flow}} if flow else None,
                },
            }
        )
    return nodes, bool(runs) and all(
        each.get("status") == "completed" for each in (*runs, *flows_seen)
    )


def rest_threads(number: int) -> dict[str, Any]:
    """A PR's review threads, whether each is resolved, from the review-thread route a cloud session's GitHub proxy
    serves. GitHub's own REST has none and answers 404: there, as for a job token in a private organization repository,
    whose GraphQL withholds other fields of PULL but not these, the threads come from GraphQL alone. Where that fails
    too, the error raises: unresolved threads are never read as none. Each thread's comments, which the proxy's route
    names by id alone, come from the PR's review comments."""
    try:
        threads = rest(f"repos/{{owner}}/{{repo}}/pulls/{number}/ccr/review_threads")
    except subprocess.CalledProcessError as exc:
        if "HTTP 404" not in (exc.stderr or ""):
            raise
        found: dict[str, Any] = _graphql(
            _repository(f"pullRequest(number: $n) {{ {THREADS} }}"), n=number
        )
        return found["pullRequest"]["reviewThreads"]
    listed = {found["id"]: found for found in rest_pages(f"pulls/{number}/comments?per_page=100")}
    nodes = [
        {
            "isResolved": bool(thread.get("resolved")),
            "comments": {
                "nodes": [
                    {
                        "body": found.get("body") or "",
                    }
                    for found in (listed.get(each) for each in thread.get("comment_ids") or [])
                    if found is not None
                ]
            },
        }
        for thread in threads
    ]
    return {"totalCount": len(nodes), "nodes": nodes}


def rest_pull(pr: int) -> dict[str, Any]:
    """PR #pr as PULL reads it, from REST, call by call. `closingIssuesReferences` is empty, since REST has no such
    link: the PR's issues are its body's `Closes`, `Part of` and `Implements` lines (closing_issues). Whether it is in
    a merge queue can't be read: `isInMergeQueue` is None, which unsignable() refuses unless a cloud session's loop.md
    says `merge_queue: false`."""
    found = rest(f"repos/{{owner}}/{{repo}}/pulls/{pr}")
    base, head = found["base"], found["head"]
    events = rest_pages(f"issues/{pr}/timeline?per_page=100")
    reviews = [
        {
            "author": actor(review.get("user")),
            "state": review["state"],
            "body": review.get("body") or "",
            "submittedAt": review.get("submitted_at"),
            "commit": {"oid": review.get("commit_id") or ""},
        }
        for review in rest_pages(f"pulls/{pr}/reviews?per_page=100")
    ]
    files = [
        {
            "path": file["filename"],
            "changeType": CHANGE_TYPES.get(file["status"], "MODIFIED"),
            "additions": file.get("additions"),
            "deletions": file.get("deletions"),
        }
        for file in pr_changes(pr)
    ]
    state = "MERGED" if found.get("merged") else found["state"].upper()
    return {
        "id": found["node_id"],
        "number": found["number"],
        "url": found.get("html_url", ""),
        "title": found["title"],
        "isCrossRepository": (head.get("repo") or {}).get("full_name") != base["repo"]["full_name"],
        "author": actor(found.get("user")),
        "assignees": {"nodes": [actor(user) for user in found.get("assignees") or []]},
        "state": state,
        "isDraft": bool(found.get("draft")),
        # REST's true or false, or null while GitHub computes it, as GraphQL's MergeableState reads them
        "mergeable": {True: "MERGEABLE", False: "CONFLICTING"}.get(
            found.get("mergeable"), "UNKNOWN"
        ),
        "autoMergeRequest": {"enabledAt": None} if found.get("auto_merge") else None,
        "isInMergeQueue": None,
        "body": found.get("body") or "",
        "baseRefName": base["ref"],
        # the base branch's tip now, as GraphQL's baseRefOid is; REST's base.sha is the tip when the PR last synced
        "baseRefOid": rest(f"repos/{{owner}}/{{repo}}/branches/{base['ref']}")["commit"]["sha"],
        "headRefName": head["ref"],
        "headRefOid": head["sha"],
        "baseRepository": {
            "nameWithOwner": base["repo"]["full_name"],
            "defaultBranchRef": {"name": base["repo"]["default_branch"]},
        },
        "labels": {"nodes": [{"name": label["name"]} for label in found.get("labels") or []]},
        "comments": rest_notes(pr),
        "files": {"totalCount": found.get("changed_files") or len(files), "nodes": files},
        "latestReviews": {"nodes": rest_latest(reviews)},
        "reviews": {"totalCount": len(reviews), "nodes": reviews},
        "reviewThreads": rest_threads(pr),
        "authored": rest_authored(pr, found.get("commits") or 0),
        "timelineItems": rest_timeline(pr, events),
        "commits": {"nodes": [{"commit": {"statusCheckRollup": rest_checks(head["sha"])}}]},
        "closingIssuesReferences": {"nodes": []},
        **rest_queue(found, events),
    }


# the REST timeline's merge-queue events
QUEUE_ADDED, QUEUE_REMOVED = "added_to_merge_queue", "removed_from_merge_queue"
# the check run conclusions that make a merge-queue group red: a run cancelled there (its group rebuilt, or the entry
# dequeued) failed nothing
GROUP_RED = {"FAILURE", "TIMED_OUT", "STARTUP_FAILURE"}


def rest_queue(found: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    """The PR's `mergeQueueEntry` and `removals` as PULL reads them, from REST, which has neither: `found`, its REST
    pull, and `events`, its timeline. The entry, only while the timeline's newest queue event is an add (GitHub may
    delete a group's ref a while after it took the PR out): awaiting checks on its group's commit while GitHub's
    readonly ref for it (`gh-readonly-queue/<base>/pr-<n>-<sha>`) exists, else queued; none, and no removal, for a PR
    whose timeline has no queue event.
    A `removed_from_merge_queue` event names no reason, so each gets GraphQL's from what REST reads on its group (the
    commit of the newest `merge_group` run on such a ref since the PR's add before it): `merged`, the last one, when
    GitHub reports the PR merged or the group's commit is in the base (GitHub takes it out a moment before it reports
    the merge); `failed_checks` when a check the base requires failed on the group's commit; `merge_conflict`, the last
    one, when GitHub reports the PR conflicting or is still working that out; else `unknown`, which counts for nothing, as `checks_timed_out` and
    `manual` do."""
    number, base, merged = found["number"], found["base"]["ref"], bool(found.get("merged"))
    prefix = f"gh-readonly-queue/{base}/pr-{number}-"
    queue = [event for event in events if event.get("event") in (QUEUE_ADDED, QUEUE_REMOVED)]
    entry = None
    if not queue:  # never in a merge queue: no ref, no group
        return {"mergeQueueEntry": entry, "removals": {"nodes": []}}
    if not merged and queue[-1]["event"] == QUEUE_ADDED:
        entry = {"state": "QUEUED", "position": None, "headCommit": None}
        if refs := rest_pages(f"git/matching-refs/heads/{prefix}"):
            group = refs[-1]["object"]["sha"]
            made = rest(f"repos/{{owner}}/{{repo}}/commits/{group}")["commit"]["committer"]["date"]
            suites = rest(f"repos/{{owner}}/{{repo}}/commits/{group}/check-suites?per_page=1")
            commit = {
                "committedDate": made,
                "checkSuites": {"totalCount": suites.get("total_count") or 0},
            }
            entry = {"state": "AWAITING_CHECKS", "position": None, "headCommit": commit}
    removed = [event for event in queue if event["event"] == QUEUE_REMOVED]
    if not removed:
        return {"mergeQueueEntry": entry, "removals": {"nodes": []}}
    # ponytail: the repo's newest 100 `merge_group` runs; a removal whose group fell out of them counts for nothing
    runs = rest("repos/{owner}/{repo}/actions/runs?event=merge_group&per_page=100")
    groups = sorted(
        (run.get("created_at") or "", run["head_sha"])
        for run in runs.get("workflow_runs") or []
        if (run.get("head_branch") or "").startswith(prefix)
    )
    required = required_checks(base) if groups else set()
    adds = [event.get("created_at") or "" for event in queue if event["event"] == QUEUE_ADDED]
    nodes = []
    for event in removed:
        at, last = event.get("created_at") or "", event is removed[-1]
        since = max((added for added in adds if added <= at), default="")
        group = next((sha for made, sha in reversed(groups) if since <= made <= at), None)
        reason, commit = "unknown", group
        if last and merged:
            reason, commit = "merged", found.get("merge_commit_sha") or group
        elif group and group_red(group, required):
            reason = "failed_checks"
        elif last and group and in_base(base, group):
            reason = "merged"
        # false: conflicting; null: GitHub working it out again, which the gatekeeper waits on, as on a host
        elif last and found.get("mergeable") is not True:
            reason = "merge_conflict"
        nodes.append(
            {"reason": reason, "createdAt": at, "beforeCommit": {"oid": commit} if commit else None}
        )
    return {"mergeQueueEntry": entry, "removals": {"nodes": nodes}}


def group_red(group: str, required: set[str]) -> bool:
    """Whether a check the base requires (any, where it requires none) failed on the merge queue's commit `group`."""
    return any(
        run["conclusion"] in GROUP_RED
        for name, run in commit_checks(group).items()
        if not required or name in required
    )


def in_base(base: str, commit: str) -> bool:
    """Whether `commit` is in `base` already, False where GitHub can't compare them."""
    try:
        return merge_base(base, commit) == commit
    except subprocess.CalledProcessError:
        return False


def pull_status(pr: int) -> dict[str, Any]:
    """What a caller that only reads a PR's status needs (`state`, `baseRefName`, `headRefOid`, `mergeable`), from one
    REST call in PULL's shape. GraphQL's PULL is dozens of fields and counts toward the shared GraphQL limit; REST's
    `pulls/N` has these four. Anything else (comments, reviews, labels, checks) stays on pull_request()."""
    found = rest(f"repos/{{owner}}/{{repo}}/pulls/{pr}")
    return {
        "number": found["number"],
        "state": "MERGED" if found.get("merged") else found["state"].upper(),
        "baseRefName": found["base"]["ref"],
        "headRefOid": found["head"]["sha"],
        "mergeable": {True: "MERGEABLE", False: "CONFLICTING"}.get(
            found.get("mergeable"), "UNKNOWN"
        ),
    }


QUEUE_WATCH = "state headRefOid isInMergeQueue labels(first: 50) { nodes { name } }"


def queue_watch(numbers: Sequence[int]) -> dict[int, dict[str, Any]]:
    """What a wait on PRs a merge queue holds reads each poll (#2101), by PR: its `state` (`OPEN`, `MERGED`,
    `CLOSED`), `headRefOid`, whether GitHub's merge queue holds it (`isInMergeQueue`) and its label names (`labels`,
    which Mergify's queue moves). GraphQL's answer is one call for every PR; REST's is two ETag reads per PR, its
    pull and its timeline, which GitHub answers 304, at no quota, while nothing changed. ponytail: REST reads the
    timeline's first 100 events; read its last page when a queued PR outgrows it (the wait's FULL round still sees a
    removal there)."""

    def graphql() -> dict[int, dict[str, Any]]:
        fields = " ".join(f"p{n}: pullRequest(number: {n}) {{ {QUEUE_WATCH} }}" for n in numbers)
        found = _graphql(
            f"query($owner: String!, $name: String!) {{ repository(owner: $owner, name: $name) {{ {fields} }} }}"
        )
        return {
            n: {
                "state": found[f"p{n}"]["state"],
                "headRefOid": found[f"p{n}"]["headRefOid"],
                "isInMergeQueue": bool(found[f"p{n}"].get("isInMergeQueue")),
                "labels": {each["name"] for each in found[f"p{n}"]["labels"]["nodes"]},
            }
            for n in numbers
        }

    def rest_read() -> dict[int, dict[str, Any]]:
        states = {}
        for n in numbers:
            _, found = rest_if_changed(f"pulls/{n}")
            _, events = rest_if_changed(f"issues/{n}/timeline?per_page=100")
            queue = [
                event.get("event")
                for event in events or []
                if isinstance(event, dict) and event.get("event") in (QUEUE_ADDED, QUEUE_REMOVED)
            ]
            states[n] = {
                "state": "MERGED" if found.get("merged") else found["state"].upper(),
                "headRefOid": found["head"]["sha"],
                "isInMergeQueue": bool(queue) and queue[-1] == QUEUE_ADDED,
                "labels": {label["name"] for label in found.get("labels") or []},
            }
        return states

    return fast(graphql, rest_read)


def pull_request(pr: int) -> dict[str, Any]:
    return fast(
        lambda: _graphql(_repository(f"pullRequest(number: $n) {{ {PULL} }}"), n=pr)["pullRequest"],
        lambda: rest_pull(pr),
    )


def pr_changes(number: int) -> list[dict[str, Any]]:
    """Every file a PR changes, with its patch, as GitHub's REST API pages them: up to 3000. Read once per run
    (`files_read`), then answered from memory."""
    if number not in files_read:
        files_read[number] = rest_pages(f"pulls/{number}/files?per_page=100")
    return files_read[number]


def merged_pull(number: int) -> dict[str, Any] | None:
    """PR #number as GitHub's REST API returns it, when it was merged; None for an open or closed PR, or an issue."""
    try:
        found: dict[str, Any] = json.loads(gh("api", f"repos/{{owner}}/{{repo}}/pulls/{number}"))
    except subprocess.CalledProcessError as exc:
        if "Not Found" in exc.stderr:
            return None
        raise
    return found if found.get("merged") else None


def merge_base(base: str, head: str) -> str:
    """The commit GitHub diffs a PR's head against: where it branched from its base."""
    return gh(
        "api", f"repos/{{owner}}/{{repo}}/compare/{base}...{head}", "--jq", ".merge_base_commit.sha"
    ).strip()


# GitHub's answer to a rules read on a private repo of a free plan
PLAN_LIMIT = "Upgrade to GitHub Pro"


class Required(set):
    """The checks a base requires (required_checks), and `queue`: the merge method (`MERGE`, `SQUASH` or `REBASE`) of
    the GitHub merge queue its rules require, None without one, which picks land's local merge queue."""

    queue: str | None = None


def fresh(key: tuple[str, str]) -> Any | None:
    """The answer `kept` holds for `key` while it is younger than KEPT, else None."""
    if (found := kept.get(key)) and time.monotonic() - found[0] < KEPT:
        return found[1]
    return None


def required_checks(base: str) -> Required:
    """The checks the base branch requires, from its live rules: its rulesets' and its branch protection's, with the
    merge method of a `merge_queue` rule as `queue`, which picks land's GitHub merge queue. A private repo on GitHub's
    free plan has no rulesets: that 403 reads as none, and any other error raises. Read once per base and KEPT
    seconds (#1771): a pass of land asks it many times, and a rule changes far less often; a copy goes out, so a
    caller's edit never reaches the kept one."""
    found: Required | None = fresh(("required", base))
    if found is None:
        found = read_required(base)
        kept[("required", base)] = (time.monotonic(), found)
    copy = Required(found)
    copy.queue = found.queue
    return copy


def read_required(base: str) -> Required:
    try:
        rulesets = gh(
            "api",
            "--paginate",
            f"repos/{{owner}}/{{repo}}/rules/branches/{base}",
            "--jq",
            '.[] | if .type == "merge_queue" then ":merge_queue:" + (.parameters.merge_method // "MERGE")'
            ' elif .type == "required_status_checks" then .parameters.required_status_checks[].context else empty end',
        )
    except subprocess.CalledProcessError as error:
        if "(HTTP 403)" not in (error.stderr or "") or PLAN_LIMIT not in error.stderr:
            raise
        print(
            f"GitHub enforces no rules on {base} here (its plan has none): land's proofs are the only gate"
        )
        rulesets = ""
    protection = gh(
        "api",
        f"repos/{{owner}}/{{repo}}/branches/{base}",
        "--jq",
        ".protection.required_status_checks.contexts // [] | .[]",
    )
    queue = [word for word in rulesets.split() if word.startswith(":merge_queue")]
    found = Required(set((rulesets + protection).split()) - set(queue))
    if queue:
        found.queue = queue[0].rpartition(":")[2]
    return found


def tree(sha: str) -> dict[str, tuple[str, str]] | None:
    """The (mode, blob SHA) of every path in commit `sha`'s tree, from GitHub's trees API; None when GitHub truncates it.
    ponytail: one recursive read, up to GitHub's 100,000 entries; read each touched directory when a repo outgrows it."""
    found = json.loads(gh("api", f"repos/{{owner}}/{{repo}}/git/trees/{sha}?recursive=1"))
    if found["truncated"]:
        return None
    return {
        entry["path"]: (entry["mode"], entry["sha"])
        for entry in found["tree"]
        if entry["type"] != "tree"
    }


def head_pushed(pull: dict[str, Any]) -> str | None:
    """When the head commit was last pushed to the PR's branch, from GitHub's repository activity (pushes, force
    pushes, the branch's creation); None when it lists none. ponytail: the newest 100 activities of a same-repo branch;
    page further, or read a fork's, when a PR here needs it."""
    out = gh(
        "api",
        f"repos/{{owner}}/{{repo}}/activity?ref=refs/heads/{pull['headRefName']}&per_page=100",
        "--jq",
        f'[.[] | select(.after == "{pull["headRefOid"]}") | .timestamp] | first // ""',
    ).strip()
    return out or None


def issue_node(number: int) -> dict[str, Any]:
    def rest_read() -> dict[str, Any]:
        found = rest_issue(number)
        if found is None:
            raise Refused(f"#{number} is a pull request, not an issue")
        return found

    node: dict[str, Any] = fast(
        lambda: _graphql(_repository(f"issue(number: $n) {{ {ISSUE} }}"), n=number)["issue"],
        rest_read,
    )
    return node


def label(number: int, name: str, add: bool) -> None:
    """Add (GitHub creates a missing label) or remove a label on an issue or PR; removing an absent one is fine."""
    path = f"repos/{{owner}}/{{repo}}/issues/{number}/labels"
    if add:
        gh("api", path, "-f", f"labels[]={name}", "--silent")
        return
    try:
        gh("api", "-X", "DELETE", f"{path}/{name}", "--silent")
    except subprocess.CalledProcessError as exc:
        if "Label does not exist" not in exc.stderr:
            raise


def assign(number: int, login: str, add: bool) -> None:
    """Add or remove an assignee of an issue or PR; GitHub ignores an absent one."""
    gh(
        "api",
        *(() if add else ("-X", "DELETE")),
        f"repos/{{owner}}/{{repo}}/issues/{number}/assignees",
        "-f",
        f"assignees[]={login}",
        "--silent",
    )


def close_issue(number: int) -> None:
    """Close an issue as completed; closing a closed one is fine."""
    gh(
        "api",
        "-X",
        "PATCH",
        f"repos/{{owner}}/{{repo}}/issues/{number}",
        "-f",
        "state=closed",
        "--silent",
    )


def comment(number: int, body: str) -> str:
    """Post `body` on PR or issue `number`; the new comment's URL."""
    return gh(
        "api",
        f"repos/{{owner}}/{{repo}}/issues/{number}/comments",
        "-F",
        "body=@-",
        "--jq",
        ".html_url",
        data=public(body),
    ).strip()


# the repository's text issue fields, which its owner's account defines; ponytail: the first 100
TEXT_FIELDS = "issueFields(first: 100) { nodes { ... on IssueFieldText { id name } } }"


def issue_field(number: int, name: str) -> tuple[str | None, str]:
    """(the id of the repository's text issue field `name`, None when the account defines none by that name; issue
    `number`'s node id), in one GraphQL read. Issue fields are GitHub's own, outside the tracker port, like the board."""
    found = _graphql(_repository(f"{TEXT_FIELDS} issue(number: $n) {{ id }}"), n=number)
    fields = ((found.get("issueFields") or {}).get("nodes")) or []
    ids = [field["id"] for field in fields if field and field.get("name") == name]
    return (ids[0] if ids else None), found["issue"]["id"]


def set_issue_field(issue: str, field: str, text: str) -> None:
    """Set the text issue field `field` (its id) on the issue whose node id is `issue`; GitHub sends no notification
    and leaves the issue's body alone."""
    gh(
        "api",
        "graphql",
        "-f",
        "query=mutation($issue: ID!, $field: ID!, $text: String!) { setIssueFieldValue(input: {issueId: $issue,"
        " issueFields: [{fieldId: $field, textValue: $text}]}) { clientMutationId } }",
        "-f",
        f"issue={issue}",
        "-f",
        f"field={field}",
        "-f",
        f"text={text}",
        "--silent",
    )


# the REST version whose issues carry `issue_field_values`, the one GitHub's issue field docs name
FIELDS_VERSION = "X-GitHub-Api-Version: 2026-03-10"


def rest_has_issue_field(name: str) -> bool:
    """Whether the repository has the issue field `name`, from REST: GitHub answers a list filtered on a field it has
    (`issue_field_values=<slug>:<value>`) and refuses one it lacks with a 422. One call, for a token GraphQL refuses."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    try:
        gh(
            "api",
            "-X",
            "GET",
            "-H",
            FIELDS_VERSION,
            "repos/{owner}/{repo}/issues",
            "-f",
            f"issue_field_values={slug}:-",
            "-f",
            "per_page=1",
            "--silent",
        )
    except subprocess.CalledProcessError as exc:
        if "HTTP 422" in (exc.stderr or ""):
            return False
        raise
    return True


def rest_issue_field_id(name: str) -> int | None:
    """The database id of the issue field `name`, read off the newest 100 issues updated, which REST shows only on an
    issue carrying a value: no REST route lists a personal account's fields. None when none of them carries it."""
    found = json.loads(
        gh(
            "api",
            "-X",
            "GET",
            "-H",
            FIELDS_VERSION,
            "repos/{owner}/{repo}/issues",
            "-f",
            "state=all",
            "-f",
            "sort=updated",
            "-f",
            "per_page=100",
        )
    )
    ids = [
        value["issue_field_id"]
        for item in found
        for value in item.get("issue_field_values") or []
        if value.get("issue_field_name") == name
    ]
    return int(ids[0]) if ids else None


def rest_set_issue_field(number: int, field: int, text: str) -> None:
    """Set the text issue field whose database id is `field` on issue `number`, over REST (POST adds or replaces this
    field's value and leaves the issue's other fields)."""
    gh(
        "api",
        "-X",
        "POST",
        "-H",
        FIELDS_VERSION,
        f"repos/{{owner}}/{{repo}}/issues/{number}/issue-field-values",
        "--input",
        "-",
        "--silent",
        data=json.dumps({"issue_field_values": [{"field_id": field, "value": text}]}),
    )


def minimize(comment_id: str) -> None:
    """Collapse a comment on GitHub as outdated; it stays readable, and the gate still reads it."""
    gh(
        "api",
        "graphql",
        "-f",
        "query=mutation($id: ID!) { minimizeComment(input: {subjectId: $id, classifier: OUTDATED}) { clientMutationId } }",
        "-f",
        f"id={comment_id}",
        "--silent",
    )


viewer_login: dict[
    str, str
] = {}  # the login `gh` runs as, read once per process (viewer); "" once GitHub refused it


def viewer() -> str | None:
    """The login of the account `gh` runs as; None for a token `GET /user` refuses, such as a workflow's installation
    token (`github.token`), where the PR's author and the repo's writers are the ones trusted (passes)."""
    return whoami()[0]


def whoami() -> tuple[str | None, bool]:
    """viewer's login, and whether GitHub answered. Either answer is read once per process: a login, or a refusal
    (definite: no rate limit), which is the token's, and a process never changes its token (#2101: a token whose
    `GET /user` is refused was asked four times a round). Who may write is writer's, kept KEPT seconds, so a grant
    later in a wait of hours is still seen. Any other failure (a network error, a rate limit) is no answer, and gh()
    keeps a rate-limited one for the round only."""
    if (login := viewer_login.get("login")) is not None:
        return login or None, True
    try:
        login = viewer_login["login"] = gh("api", "user", "--jq", ".login").strip()
    except subprocess.CalledProcessError as exc:
        if refused(exc.stderr) and not throttled(exc.stderr):
            viewer_login["login"] = ""  # refused: no login, for the process
            return None, True
        return (
            None,
            False,
        )  # not kept: a network or rate-limit failure must not read as a token GitHub refuses
    return login, True


def writer(login: str) -> bool:
    """Whether `login` can write to the repo, by GitHub's collaborator permission; an account GitHub refuses to look
    up can't. ponytail: a bot's GraphQL login lacks the `bot` REST needs, so a bot author fails closed; add the
    suffix when a project's bot opens sub-issues. GitHub's answer is kept KEPT seconds (#1771), since a pass of land
    asks it for every login it trusts, a refusal too, so a wait sees a grant within KEPT; a failure that is no answer
    (a network error, a rate limit) is not kept."""
    found: bool | None = fresh(("writer", login))
    if found is None:
        found, answered = read_writer(login)
        if answered:
            kept[("writer", login)] = (time.monotonic(), found)
    return found


def read_writer(login: str) -> tuple[bool, bool]:
    """writer's answer, and whether GitHub gave it: a permission read, a lookup GitHub says has no such account (404),
    or, where the lookup is refused, the repo's own permissions for the account running this."""
    try:
        permission = gh(
            "api",
            f"repos/{{owner}}/{{repo}}/collaborators/{login}/permission",
            "--jq",
            ".permission",
        )
    except subprocess.CalledProcessError as exc:
        # a cloud session's proxy refuses this path; the repo's own permissions answer it for the account running this
        # alone, and anyone else still can't, so a stranger never counts as a writer
        if not refused(exc.stderr) or throttled(exc.stderr):
            return False, "HTTP 404" in (exc.stderr or "")
        me = viewer()
        if me != login:
            # a viewer() that failed is no answer about `login`; one GitHub refused is
            return False, me is not None or viewer_login.get("login") == ""
        try:
            mine = rest("repos/{owner}/{repo}").get("permissions") or {}
        except subprocess.CalledProcessError:
            return False, False
        return bool(mine.get("push") or mine.get("maintain") or mine.get("admin")), True
    return permission.strip() in WRITE, True


def current_pr() -> int | None:
    def graphql() -> int | None:
        try:
            return int(gh("pr", "view", "--json", "number", "--jq", ".number"))
        except subprocess.CalledProcessError as exc:
            if "no pull requests found" in exc.stderr:
                return None
            raise

    def rest_read() -> int | None:
        # the open PR whose head is this branch of origin's repo, as `gh pr view` finds it
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], check=True, capture_output=True, text=True
        ).stdout.strip()
        owner = rest("repos/{owner}/{repo}")["owner"]["login"]
        found = rest_pages(f"pulls?state=open&head={owner}:{branch}&per_page=100")
        return int(found[0]["number"]) if found else None

    return fast(graphql, rest_read)


# a remote URL's host and `owner/repo`: `scheme://[user@]host[:port]/owner/repo` or scp-like `[user@]host:owner/repo`,
# its path maybe led by extra slashes as gh allows, matched once trailing slashes and `.git` are cut (post_demo.sh
# parses it the same way)
REMOTE_URL = re.compile(
    r"(?:[a-z][a-z0-9+.-]*://)?(?:[^@/]*@)?([^/:]+)(?::\d+)?[:/]/*([^/]+/[^/]+)", re.IGNORECASE
)


def github_repo(url: str) -> str | None:
    """The `owner/repo` a GitHub remote URL names, lowercased; None for another host."""
    found = REMOTE_URL.fullmatch(re.sub(r"(?:\.git)?/*$", "", url.rstrip("/")))
    return found[2].lower() if found and found[1].lower() == "github.com" else None


def one_repo() -> None:
    """Refuse a checkout whose remotes name several GitHub repos while none is gh's default (`gh repo set-default`,
    kept as `remote.<name>.gh-resolved`): gh then reads `{owner}/{repo}` and every `gh pr` as `upstream`, and a
    review or label lands on a repo nobody chose. Outside a checkout it passes, and gh says why it can't run."""
    remotes = subprocess.run(["git", "remote", "-v"], capture_output=True, text=True, check=False)
    repos = sorted(
        {
            repo
            for line in remotes.stdout.splitlines()
            if len(fields := line.split()) > 1 and (repo := github_repo(fields[1]))
        }
    )
    chosen = subprocess.run(
        ["git", "config", "--get-regexp", r"^remote\..*\.gh-resolved$"],
        capture_output=True,
        check=False,
    )
    if len(repos) > 1 and chosen.returncode:
        raise Refused(
            f"this checkout's remotes name {', '.join(repos)} and gh has no default among them, so gh would pick one"
            " itself: run `gh repo set-default <owner>/<repo>` once, naming the repo the PRs live in"
        )


def diff_lines(base: str, head: str) -> dict[str, list[int]]:
    """The lines a review at `head` can comment on, per file: the new side of GitHub's diff of base...head (added and
    context lines). ponytail: one compare, up to the 300 files it lists."""
    out = gh(
        "api",
        f"repos/{{owner}}/{{repo}}/compare/{base}...{head}",
        "--jq",
        ".files[] | {filename, patch}",
    )
    found: dict[str, list[int]] = {}
    for entry in (json.loads(line) for line in out.splitlines()):
        lines, number = found.setdefault(entry["filename"], []), 0
        for line in (entry.get("patch") or "").split("\n"):
            hunk = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", line)
            if hunk:
                number = int(hunk.group(1))
            elif line[:1] in (" ", "+"):
                lines.append(number)
                number += 1
    return found


def post_review(pr: int, review: dict[str, Any]) -> None:
    gh(
        "api",
        f"repos/{{owner}}/{{repo}}/pulls/{pr}/reviews",
        "--input",
        "-",
        "--silent",
        # unescaped, so public() finds a non-ASCII home as GitHub will decode it
        data=public(json.dumps(review, ensure_ascii=False)),
    )


def statuses(head: str) -> list[dict[str, Any]]:
    """The commit statuses on `head`, newest first; ponytail: the first 100, which a head reaches only by hand."""
    out = gh("api", f"repos/{{owner}}/{{repo}}/commits/{head}/statuses?per_page=100")
    found: list[dict[str, Any]] = json.loads(out)
    return found


def post_status(head: str, context: str, description: str, state: str = "success") -> None:
    gh(
        "api",
        f"repos/{{owner}}/{{repo}}/statuses/{head}",
        "-f",
        f"state={state}",
        "-f",
        f"context={context}",
        "-f",
        f"description={description}",
        "--silent",
    )


def issues(numbers: list[int]) -> list[dict[str, Any]]:
    """The issues among `numbers`, as ISSUE reads them, in one call; a number that is a pull request is left out."""
    aliases = " ".join(
        f"i{n}: issueOrPullRequest(number: {n}) {{ ... on Issue {{ {ISSUE} }} }}" for n in numbers
    )
    found = fast(
        lambda: list(
            _graphql(
                f"query($owner: String!, $name: String!) {{ repository(owner: $owner, name: $name) {{ {aliases} }} }}"
            ).values()
        ),
        lambda: [rest_issue(n) for n in numbers],
    )
    return [issue for issue in found if issue]


def default_branch() -> str:
    """The repository's default branch."""
    found: str = fast(
        lambda: gh(
            "repo", "view", "--json", "defaultBranchRef", "--jq", ".defaultBranchRef.name"
        ).strip(),
        lambda: rest("repos/{owner}/{repo}")["default_branch"],
    )
    return found


def ready(number: int, undo: bool = False) -> None:
    """Mark PR #number ready for review, or with `undo` back to draft: over GraphQL, else the draft routes a cloud
    session's proxy serves."""
    fast(
        lambda: gh("pr", "ready", *(("--undo",) if undo else ()), str(number)),
        lambda: gh(
            "api",
            "-X",
            "POST",
            f"repos/{{owner}}/{{repo}}/pulls/{number}/ccr/{'convert_to_draft' if undo else 'ready_for_review'}",
            "--silent",
        ),
    )


# the statuses the loop posts itself, which a base's required checks never wait for: gatekeeper.py's OWN, set when it
# loads this module, so the two never differ (empty until then, which only makes a required check of one of them count)
OWN_CHECKS: frozenset[str] = frozenset()
GREEN_CHECKS = frozenset({"SUCCESS", "NEUTRAL", "SKIPPED"})


def red_checks(number: int, head: str) -> list[str]:
    """A line per check PR #number's base requires that is not green on `head`, red, pending or missing: empty when every
    one is. A check run answers by its conclusion, a commit status (the newest of its context) by its state."""
    required = required_checks(pull_status(number)["baseRefName"]) - OWN_CHECKS
    runs = commit_checks(head)
    marks: dict[str, str] = {}
    for mark in statuses(head):  # newest first
        marks.setdefault(mark["context"], mark["state"])
    found = []
    for name in sorted(required):
        if name in runs:
            state = runs[name]["conclusion"] or "PENDING"
            green = state in GREEN_CHECKS
        elif name in marks:
            state, green = marks[name].upper(), marks[name] == "success"
        else:
            state, green = "missing", False
        if not green:
            found.append(f"`{name}` is {state}")
    return found


# the two ways a cloud session's proxy answers the REST merge with a 403, by the words of its body
PROXY_PATH = "not permitted through this proxy"
PROXY_UNATTACHED = "not enabled for this session"


def merge_refusal(number: int, stderr: str | None) -> str | None:
    """What to do when the proxy refused PR #number's REST merge, None for any other error (a 409 or 422 is a stale
    head or a conflict). A path rule is per session or environment, so attaching the repo can't lift it: the land
    runs from a host checkout. A repo the session has no access to is the one `add_repo` fixes. Whether a proxy will
    refuse can't be told without a write, so this only maps the answer."""
    text = " ".join((stderr or "").split())
    if "HTTP 403" not in text:
        return None
    if PROXY_PATH in text:
        return (
            f"PR #{number}: this cloud session's proxy refuses the merge call for this repo ({text}); it is a path rule,"
            " and attaching the repo does not lift it. Run `land` from a host checkout of the repo (a clone of it on"
            " a machine whose own GitHub login merges), where the same PR lands at the same head"
        )
    if PROXY_UNATTACHED in text:
        return (
            f"PR #{number}: this cloud session has no access to the repo ({text}); attach it with push access"
            ' (`add_repo`, `access: "push"`) and land again, or run `land` from a host checkout'
        )
    return None


def merge(number: int, method: str, head: str) -> None:
    """Merge PR #number by `method` (merge, squash or rebase), pinned to `head`, with GitHub's default body. A base requiring a merge queue only queues it. Where GitHub refuses GraphQL, which
    `gh pr merge` goes over, a cloud session's proxy for one, the same merge goes over REST (`PUT pulls/N/merge`, whose
    `sha` is the pin). Refused while a required check is not green on `head`,
    whether or not the owner approved a break-glass: that PR is merged only by the owner in the GitHub UI (#1722)."""
    if held := red_checks(number, head):
        raise Refused(
            f"PR #{number}: refusing to merge while a required check is not green on {head}: {'; '.join(held)}"
            " (a break-glass PR is merged by the owner in the GitHub UI, never by a helper)"
        )

    def rest_merge() -> str:
        try:
            return gh(
                "api",
                "-X",
                "PUT",
                f"repos/{{owner}}/{{repo}}/pulls/{number}/merge",
                "-f",
                f"merge_method={method}",
                "-f",
                f"sha={head}",
            )
        except subprocess.CalledProcessError as exc:
            if (why := merge_refusal(number, exc.stderr)) is None:
                raise
            raise Refused(why) from exc

    fast(
        lambda: gh(
            "pr",
            "merge",
            str(number),
            f"--{method}",
            "--match-head-commit",
            head,
        ),
        rest_merge,
    )


def run_info(run: int) -> dict[str, Any]:
    """Actions run `run` as GitHub's REST API reads it: its repository, workflow, head commit, conclusion and times."""
    found: dict[str, Any] = json.loads(gh("api", f"repos/{{owner}}/{{repo}}/actions/runs/{run}"))
    return found


def run_jobs(run: int) -> list[dict[str, Any]]:
    """The jobs of Actions run `run`, each with its name, conclusion and steps."""
    return rest_pages(f"actions/runs/{run}/jobs?per_page=100", ".jobs[]")


def workflow_runs(
    workflow: int, branch: str | None = None, head: str | None = None
) -> list[dict[str, Any]]:
    """The newest 100 completed runs of workflow `workflow`, on `branch` or on the commit `head`, newest first."""
    query = "status=completed&per_page=100" + (f"&branch={branch}" if branch else "")
    query += f"&head_sha={head}" if head else ""
    found: list[dict[str, Any]] = json.loads(
        gh("api", f"repos/{{owner}}/{{repo}}/actions/workflows/{workflow}/runs?{query}")
    )["workflow_runs"]
    return found


def run_artifact(run: int, name: str) -> dict[str, Any] | None:
    """The JSON file inside run `run`'s artifact `name` (its first `.json` member), None when the run has no such
    artifact or it holds no JSON. ponytail: the newest 100 artifacts of the run, one zip downloaded."""
    listed = json.loads(
        gh("api", f"repos/{{owner}}/{{repo}}/actions/runs/{run}/artifacts?per_page=100")
    )
    wanted = [a for a in listed["artifacts"] if a["name"] == name and not a.get("expired")]
    if not wanted:
        return None
    done = subprocess.run(
        ["gh", "api", f"repos/{{owner}}/{{repo}}/actions/artifacts/{wanted[0]['id']}/zip"],
        check=True,
        capture_output=True,
    )
    with zipfile.ZipFile(io.BytesIO(done.stdout)) as archive:
        for member in archive.namelist():
            if member.endswith(".json"):
                found = json.loads(archive.read(member))
                return found if isinstance(found, dict) else None
    return None


def compare(base: str, head: str) -> dict[str, Any]:
    """How `head` stands against `base`: `status` (`ahead`, `behind`, `identical`, `diverged`), `ahead_by`, `behind_by`."""
    found: dict[str, Any] = json.loads(
        gh(
            "api",
            f"repos/{{owner}}/{{repo}}/compare/{base}...{head}?per_page=1",
            "--jq",
            "{status, ahead_by, behind_by}",
        )
    )
    return found


def open_heads() -> list[str]:
    """The head commit of every open PR. ponytail: REST's pages of 100, every one."""
    # whole objects, since rest_pages decodes each line as JSON and a bare SHA is none
    return [pull["head"]["sha"] for pull in rest_pages("pulls?state=open&per_page=100")]


# the GitHub merge queue's calls: enqueue pinned to the head the proofs judged, dequeue a stuck entry
ENQUEUE = (
    "mutation($id: ID!, $head: GitObjectID!) { enqueuePullRequest(input: {pullRequestId: $id, expectedHeadOid: $head})"
    " { mergeQueueEntry { position } } }"
)
DEQUEUE = "mutation($id: ID!) { dequeuePullRequest(input: {id: $id}) { clientMutationId } }"


def enqueue_pull(number: int, node: str, head: str, method: str) -> int | None:
    """Put PR #number (`node`, its GraphQL id) in its base's merge queue pinned to `head`, so GitHub refuses it once
    the head moved: its position, None where GitHub names none. A refusal raises. Where GraphQL answers 403, auto-merge
    on with the queue's merge `method` and `head`, which on a base requiring the queue enqueues it (rest_enqueue)."""

    def graphql() -> int | None:
        out = gh(
            "api", "graphql", "-f", f"query={ENQUEUE}", "-f", f"id={node}", "-f", f"head={head}"
        )
        entry = ((json.loads(out or "{}").get("data") or {}).get("enqueuePullRequest") or {}).get(
            "mergeQueueEntry"
        )
        position: int | None = (entry or {}).get("position")
        return position

    return fast(graphql, lambda: rest_enqueue(number, head, method))


def auto_merge(number: int) -> str:
    """The auto-merge route a cloud session's proxy serves for PR #number."""
    return f"repos/{{owner}}/{{repo}}/pulls/{number}/ccr/auto_merge"


def rest_enqueue(number: int, head: str, method: str) -> None:
    """Auto-merge on for PR #number with `method` (lower case) and `head` as the expected head, then the PR read again:
    a head other than `head` is taken out again and Refused, naming both, since the route may ignore the head it was
    sent. ponytail: a push between the enable and the read that the queue merges before the dequeue lands unseen;
    GitHub drops an entry whose head moves, so that needs a push and a whole `merge_group` run in one REST round trip."""
    gh(
        "api",
        "-X",
        "PUT",
        auto_merge(number),
        "-f",
        f"merge_method={method.lower()}",
        "-f",
        f"expected_head_oid={head}",
        "--silent",
    )
    now = rest(f"repos/{{owner}}/{{repo}}/pulls/{number}")["head"]["sha"]
    if now == head:
        return
    moved = f"its head is {now} now, not {head}, which the proofs judged"
    try:
        gh("api", "-X", "DELETE", auto_merge(number), "--silent")
    except subprocess.CalledProcessError as exc:
        raise Refused(
            f"{moved}, and land could not take it out of the merge queue: take it out on GitHub"
            f" ({' '.join((exc.stderr or '').split())})"
        ) from exc
    raise Refused(f"{moved}: taken out of the merge queue again")


def dequeue_pull(number: int, node: str) -> None:
    """Take PR #number (`node`, its GraphQL id) out of the merge queue: over GraphQL, else auto-merge off."""
    fast(
        lambda: gh("api", "graphql", "-f", f"query={DEQUEUE}", "-f", f"id={node}"),
        lambda: gh("api", "-X", "DELETE", auto_merge(number), "--silent"),
    )


def commit_checks(ref: str) -> dict[str, dict[str, Any]]:
    """The newest run of each check on commit (or branch) `ref`, by name: its conclusion as GraphQL spells it, link,
    id, which for an Actions job is the job's, and its output's title and summary. ponytail: check runs only, one per name; read status contexts and
    workflow names when a queue's required check is a status or one name runs in two workflows."""
    runs = rest_pages(f"commits/{ref}/check-runs?filter=latest&per_page=100", ".check_runs[]")
    return {
        run["name"]: {
            "conclusion": (run.get("conclusion") or "").upper(),
            "url": run.get("html_url") or "",
            "id": run.get("id"),
            "title": (run.get("output") or {}).get("title") or "",
            "summary": (run.get("output") or {}).get("summary") or "",
            "completed_at": run.get("completed_at") or "",
        }
        for run in runs
    }


def job_log(job: int) -> str:
    """An Actions job's log, by its id, which is its check run's."""
    return gh("api", f"repos/{{owner}}/{{repo}}/actions/jobs/{job}/logs")


def commit_tree(sha: str) -> str:
    """Commit `sha`'s tree SHA."""
    return gh("api", f"repos/{{owner}}/{{repo}}/commits/{sha}", "--jq", ".commit.tree.sha").strip()


def repo_tags(repo: str) -> dict[str, str]:
    """Every tag of `repo`, by name: the commit each points at (an annotated tag's too), from one paged read."""
    out = gh(
        "api", "--paginate", f"repos/{repo}/tags?per_page=100", "--jq", ".[] | [.name, .commit.sha]"
    )
    return {name: sha for name, sha in map(json.loads, out.splitlines())}


def tag_tree(repo: str, tag: str) -> tuple[str, dict[str, tuple[str, str]]] | None:
    """Tag `tag` of `repo`: its commit and the (mode, blob SHA) of every path in that commit's tree; None when the repo
    has no such tag, it names no commit, or GitHub truncates the tree."""
    try:
        found = rest(f"repos/{repo}/git/ref/tags/{tag}")["object"]
    except subprocess.CalledProcessError as exc:
        if "Not Found" in (exc.stderr or ""):
            return None
        raise
    while found["type"] == "tag":  # an annotated tag: its object
        found = rest(f"repos/{repo}/git/tags/{found['sha']}")["object"]
    if found["type"] != "commit":
        return None
    listed = rest(f"repos/{repo}/git/trees/{found['sha']}?recursive=1")
    if listed["truncated"]:
        return None
    return found["sha"], {
        entry["path"]: (entry["mode"], entry["sha"])
        for entry in listed["tree"]
        if entry["type"] != "tree"
    }


def file_at(ref: str, path: str, repo: str | None = None) -> str | None:
    """File `path`'s text at `ref` of `repo` (this checkout's when None); None when it has no such file."""
    try:
        return gh(
            "api",
            "-H",
            "Accept: application/vnd.github.raw",
            f"repos/{repo or '{owner}/{repo}'}/contents/{path}?ref={ref}",
        )
    except subprocess.CalledProcessError as exc:
        if "Not Found" in (exc.stderr or ""):
            return None
        raise


def pusher(ref: str) -> dict[str, Any] | None:
    """The newest push to the branch `ref` (`refs/heads/<name>`), from the repository's activity: `after`, the commit
    it set the branch to, `login`, the account that pushed, and `app`, true where that account is an app's (GitHub's
    actor type `Bot`, as a workflow's token pushes), which no person's token can act as; None when it lists none."""
    found = rest(
        f"repos/{{owner}}/{{repo}}/activity?ref={urllib.parse.quote(ref, safe='')}&per_page=1"
    )
    if not found:
        return None
    who = found[0].get("actor") or {}
    return {
        "after": found[0].get("after") or "",
        "login": who.get("login"),
        "app": who.get("type") == "Bot",
    }


class Port:
    """One port's operations, each this module's function of that name, looked up at the call, so a stub set on the
    module (a test's) is the one called."""

    def __init__(self, *operations: str) -> None:
        self.operations = operations

    def __getattr__(self, operation: str) -> Callable[..., Any]:
        if operation not in self.operations:
            raise AttributeError(operation)
        found: Callable[..., Any] = globals()[operation]
        return found


tracker = Port("issue_node", "issues", "comment", "label", "assign", "close_issue")
forge = Port(
    "pull_request",
    "pull_status",
    "queue_watch",
    "current_pr",
    "pr_changes",
    "merged_pull",
    "merge_base",
    "diff_lines",
    "tree",
    "head_pushed",
    "merged_labelled",
    "open_pulls",
    "open_pull_files",
    "open_heads",
    "required_checks",
    "default_branch",
    "viewer",
    "writer",
    "github_repo",
    "one_repo",
    "ready",
    "merge",
    "enqueue_pull",
    "dequeue_pull",
    "commit_checks",
    "run_info",
    "run_jobs",
    "workflow_runs",
    "run_artifact",
    "compare",
    "job_log",
    "commit_tree",
    "repo_tags",
    "tag_tree",
    "file_at",
    "pusher",
)
evidence = Port("statuses", "post_status", "post_review", "minimize")
