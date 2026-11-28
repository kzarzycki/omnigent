"""GitHub integration for the session workspace, backed by the ``gh`` CLI.

Implements GitHub's steps of the session PR panel: details, files, patches, and
the attach check. :mod:`omnigent.runner.pr_resource` selects the PR and the
provider and reaches these steps through the GitHub facet
(:mod:`omnigent.runner.git_providers.github`). The ``github_*`` functions here
delegate to ``pr_resource``. Tracked PRs use explicit host/repository/number
identities. Branch and commit discovery remains a fallback for sessions
without recorded PRs. Files and patches come from GitHub; recording an
association does not modify the remote PR.

Design notes:

- Commands run via plain :func:`subprocess.run` in the workspace root, NOT the
  sandboxed OS-env shell helper. The helper strips secrets from the environment,
  which would break ``gh`` auth; a plain subprocess inherits the runner process
  environment, so ``gh`` authenticates as it normally does — the developer's
  ``gh`` login in local dev, and in a managed sandbox the per-user ``hosts.yml``
  that :func:`omnigent.git_credential_github.configure_host_gh` writes from the
  credential broker at host startup. In a sandbox we additionally scrub
  ``GH_TOKEN``/``GITHUB_TOKEN`` from ``gh``'s env (gh ranks those *above*
  ``hosts.yml``), so a stray ambient token — e.g. a gh-MCP env passthrough —
  can't silently make the panel act as a shared identity instead of the
  connected owner. Outside a sandbox the env is inherited untouched.
- The list and patch are GitHub-computed, never a local ``git diff``, so a stale
  local ``origin/<base>`` can't inflate them with files outside the PR.
- Expanded context uses the selected PR's head and merge-base commits through
  GitHub, including fork heads and renames. Legacy calls without a session
  continue to read local git objects.
- The branch→PR lookup is a ``gh pr view --json`` (``--json`` avoids the
  interactive pager and the Projects-classic mis-parse of a bare view), backed by
  a commit-identity fallback for when the remote branch name is decoupled from the
  checkout. Fork / triangular PRs resolve once the two coordinates they turn on
  are set explicitly: the base repo (``gh repo set-default``, which ``gh`` stores
  in ``.git/config`` as ``remote.<name>.gh-resolved``) and the head owner (the
  authenticated account). The panel surfaces an account + remote selector so the
  user pins both; a per-repo account preference (``~/.omnigent/config.yaml``) is
  applied by running each ``gh`` call as the chosen account —
  ``GH_TOKEN=$(gh auth token --user <login>)`` — outside a sandbox (inside one we
  keep the single broker identity).
- When a stacking tool (git-stack, ``git pp``) pushes under a remote branch name
  that differs from the local checkout and leaves no upstream tracking, ``gh``'s
  branch-name lookup misses. The pushed commit is the invariant that still links
  the checkout to its PR, so the fallback asks GitHub which PR a pushed commit
  belongs to (``repos/{owner}/{repo}/commits/{sha}/pulls``), querying the repo it
  was pushed to (the fork for a fork PR) — see :func:`_resolve_pr_via_commit`.
- ``available: false`` payloads let the tab render a message ("gh not installed",
  "not a git repo") instead of surfacing an error.
- A token without the Checks permission (a fine-grained PAT) can read a PR but
  not its check runs: GitHub refuses just ``statusCheckRollup`` and ``gh`` fails
  the whole call, so PR reads retry without that field and report
  ``checks_supported: false`` rather than pretending there is no PR.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

try:
    import fcntl
except ImportError:  # Windows: reads are not single-flighted
    fcntl = None  # type: ignore[assignment]

from omnigent import config as _config
from omnigent.git_providers import host_of
from omnigent.git_providers.github import (
    cached_gh_token,
    forget_gh_tokens,
    gh_config_dir,
    hosts_file_stat,
)
from omnigent.host.identity_env import in_managed_sandbox
from omnigent.process_logging import data_dir
from omnigent.runner import pr_resource
from omnigent.runner.git_providers import (
    COMMIT_OBJECT,
    MERGE_OPTIONS_OBJECT,
    PR_ATTACHMENT_OBJECT,
    PR_DETAILS_OBJECT,
    PR_DIFF_OBJECT,
    PR_LABELS_OBJECT,
    PR_MERGE_OBJECT,
    PR_REVIEWERS_OBJECT,
    PullRequestStatus,
    ReviewerRef,
    local_git,
)
from omnigent.runner.session_prs import PullRequestRef
from omnigent.runtime.filesystem_registry import _git_timeout_seconds

_logger = logging.getLogger(__name__)

# ``gh pr view`` / ``gh repo view`` reach the GitHub API, so they get their own,
# slightly more generous timeout than the local ``git`` reads. Overridable via
# ``OMNIGENT_GH_TIMEOUT_SECONDS`` so operators can tune it without a restart.
_DEFAULT_GH_TIMEOUT_SECONDS = 15.0
# A request with its own time budget bounds every ``gh`` call by this monotonic
# deadline (see :func:`_gh_deadline_scope`); ``_gh_timed_out`` records a cut-off call.
_gh_deadline: ContextVar[float | None] = ContextVar("gh_deadline", default=None)
# The host the current panel step's ``gh`` calls are for, set by the provider facet.
_gh_target_host: ContextVar[str | None] = ContextVar("gh_target_host", default=None)
_gh_timed_out: ContextVar[bool] = ContextVar("gh_timed_out", default=False)

# Always pass --json (bare gh pr view opens a pager); body, comments,
# reviews, labels, and the merge fields feed the Summary and Comments tabs.
_PR_VIEW_FIELDS = (
    "number,title,state,url,isDraft,author,baseRefName,headRefName,statusCheckRollup,body,comments"
    ",reviews,reviewRequests,latestReviews,labels,mergeStateStatus,autoMergeRequest"
)
_PR_REVIEW_FIELDS = frozenset({"reviewRequests", "latestReviews"})
_PR_COMMENTS_FIELD = "reviews"
# Older ``gh`` releases lack these; :func:`_gh_pr_view` retries without them.
_PR_MERGE_FIELDS = frozenset({"mergeStateStatus", "autoMergeRequest"})
# Reading check runs needs the Checks permission, which a fine-grained personal
# access token cannot hold; GitHub refuses just this field and ``gh`` fails the
# whole call, so :func:`_gh_pr_view` retries without it.
_PR_CHECKS_FIELD = "statusCheckRollup"


def _gh_timeout_seconds() -> float:
    """Return the ``gh``-subprocess timeout, honoring the env override."""
    raw = os.environ.get("OMNIGENT_GH_TIMEOUT_SECONDS")
    if raw is not None:
        try:
            value = float(raw)
        except ValueError:
            value = 0.0
        if value > 0:
            return value
    return _DEFAULT_GH_TIMEOUT_SECONDS


def _run(
    argv: list[str],
    *,
    cwd: str,
    timeout: float,
    env: dict[str, str] | None = None,
) -> tuple[int | None, str, str]:
    """Run a subprocess and capture its output, never raising.

    :param argv: Command and arguments.
    :param cwd: Working directory to run in.
    :param timeout: Wall-clock cap in seconds.
    :param env: Full child environment, or ``None`` to inherit this process's
        (the default).
    :returns: ``(returncode, stdout, stderr)``. ``returncode`` is ``None`` when
        the command could not run at all (spawn error / timeout), so callers can
        distinguish "ran and failed" from "never ran".
    """
    started = time.monotonic()
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        _logger.warning(
            "github_resource: %r in %s timed out after %.2fs",
            argv,
            cwd,
            time.monotonic() - started,
        )
        return None, "", "timed out"
    except OSError as exc:
        _logger.warning("github_resource: %r in %s could not run: %s", argv, cwd, exc)
        return None, "", str(exc)
    return (
        result.returncode,
        result.stdout.decode("utf-8", errors="replace"),
        result.stderr.decode("utf-8", errors="replace"),
    )


def _git(argv: list[str], *, cwd: str) -> tuple[int | None, str, str]:
    return _run(["git", *argv], cwd=cwd, timeout=_git_timeout_seconds())


def _in_sandbox() -> bool:
    """Whether the panel is running inside a managed sandbox (``IS_SANDBOX=1``)."""
    return in_managed_sandbox()


@dataclass(frozen=True)
class GhInvocation:
    """The process one ``gh`` call starts.

    :ivar argv: The command and its arguments.
    :ivar env: The child's whole environment, or ``None`` to inherit this process's.
    :ivar host: The code host the call is for, from :func:`gh_target_host_scope`;
        ``None`` when the step didn't name one.
    """

    argv: list[str]
    env: dict[str, str] | None
    host: str | None = None


@contextmanager
def gh_target_host_scope(host: str | None) -> Iterator[None]:
    """Mark every ``gh`` call in the block as being for *host*.

    The value is per context, so concurrent steps in other threads keep their own.
    """
    token = _gh_target_host.set(host.lower() if host else None)
    try:
        yield
    finally:
        _gh_target_host.reset(token)


def _gh_invocation(argv: list[str], *, token: str | None, host: str | None) -> GhInvocation:
    """Return the command and environment that run ``gh`` with *argv*.

    Every ``gh`` call the panel makes starts through here.

    :param argv: Arguments after ``gh``.
    :param token: A GitHub token to run this one call as a chosen account, or ``None``.
    :param host: The code host the call is for, or ``None``; every host runs ``gh``.
    """
    # In a managed sandbox the panel must authenticate as the connected owner via
    # the per-user hosts.yml that configure_host_gh writes — never an ambient
    # GH_TOKEN/GITHUB_TOKEN, which gh ranks ABOVE hosts.yml. Scrub them so a stray
    # token in the sandbox/runner env (e.g. a gh-MCP passthrough) can't silently
    # make the panel act as a shared identity. Outside a sandbox (local dev) the
    # env is inherited untouched, so the developer's own gh auth still works.
    #
    # ``token`` deliberately re-adds GH_TOKEN to run this one call as a chosen
    # account (the account selector). It's only ever set outside a sandbox — see
    # _account_token_for — so it never overrides the sandbox's broker identity.
    env: dict[str, str] | None = None
    if _in_sandbox():
        env = {
            k: v
            for k, v in os.environ.items()
            if k
            not in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")
        }
    if token:
        env = dict(os.environ) if env is None else env
        env["GH_TOKEN"] = token
        env["GH_ENTERPRISE_TOKEN"] = token
        env.pop("GITHUB_TOKEN", None)
        env.pop("GITHUB_ENTERPRISE_TOKEN", None)
    return GhInvocation(argv=["gh", *argv], env=env, host=host)


def _gh(
    argv: list[str], *, cwd: str, token: str | None = None, timeout: float | None = None
) -> tuple[int | None, str, str]:
    # ``timeout`` replaces the per-call limit: higher for a slow call such as a video
    # upload, lower for a best-effort one.
    timeout = timeout or _gh_timeout_seconds()
    if (deadline := _gh_deadline.get()) is not None:
        timeout = min(timeout, deadline - time.monotonic())
        if timeout <= 0:
            _gh_timed_out.set(True)
            return None, "", "gh deadline exceeded"
    invocation = _gh_invocation(argv, token=token, host=_gh_target_host.get())
    result = _run(invocation.argv, cwd=cwd, timeout=timeout, env=invocation.env)
    if result[0] not in (0, None) and _AUTH_FAILURE_PATTERN.search(result[2]):
        _forget_account_lookups(token)
    if (
        deadline is not None
        and result[0] is None
        and result[2] == "timed out"
        and time.monotonic() >= deadline
    ):
        _gh_timed_out.set(True)
    return result


@contextmanager
def _gh_deadline_scope(deadline: float) -> Iterator[None]:
    """End every ``gh`` call in the block by ``deadline``, a ``time.monotonic()`` value.

    ``_gh_timed_out`` starts false and turns true once the deadline cuts a call short.
    """
    deadline_token = _gh_deadline.set(deadline)
    timed_out_token = _gh_timed_out.set(False)
    try:
        yield
    finally:
        _gh_timed_out.reset(timed_out_token)
        _gh_deadline.reset(deadline_token)


# ── Account selection ────────────────────────────────────────────────────────
# The panel offers ONE selector — the account (head owner) — since fork/triangular
# PRs resolve by pushed-commit identity and the base repo is auto-resolved from the
# PR. A per-workspace account preference runs every ``gh`` call as the chosen login;
# a sandbox has a single identity so the account override no-ops there.


def _owner_repo_from_url(url: str | None) -> str | None:
    """Derive ``owner/repo`` from a git remote URL, or ``None``.

    Handles HTTPS/SSH/scp-style GitHub URLs, stripping any ``.git`` suffix.
    """
    if not url:
        return None
    candidate = url.strip()
    scp = re.match(r"^[\w.\-]+@[\w.\-]+:(?P<path>.+)$", candidate)
    if scp:
        path = scp.group("path")
    else:
        scheme = re.match(r"^\w+://(?:[^@/]+@)?[\w.\-]+/(?P<path>.+)$", candidate)
        if not scheme:
            return None
        path = scheme.group("path")
    parts = path.removesuffix(".git").strip("/").split("/")
    if len(parts) < 2 or not parts[-1] or not parts[-2]:
        return None
    return f"{parts[-2]}/{parts[-1]}"


def _owner_repo_from_pr_url(url: str | None) -> str | None:
    """Derive the base ``owner/repo`` from a PR's HTML URL, or ``None``.

    A PR lives on its base repo, so ``https://host/<owner>/<repo>/pull/<n>`` names
    it in the first two path segments — lets us skip a separate ``gh repo view``
    once a PR resolves.
    """
    if not url:
        return None
    match = re.match(r"^https?://[^/]+/([^/]+)/([^/]+)/pull/\d+", url.strip())
    return f"{match.group(1)}/{match.group(2)}" if match else None


def _list_accounts(root: str) -> tuple[bool, list[dict[str, Any]]]:
    """Return ``(authenticated, accounts)`` from ``gh auth status --json hosts``.

    ``accounts`` is ``[{login, active, state, host}]``; ``authenticated`` is true
    when at least one account validates (``state == "success"``). Falls back to a
    plain ``gh auth status`` for the boolean on an older ``gh`` without ``--json``.
    """
    _, out, _ = _gh(["auth", "status", "--json", "hosts"], cwd=root)
    try:
        data = json.loads(out)
    except ValueError:
        data = None
    hosts = data.get("hosts") if isinstance(data, dict) else None
    if isinstance(hosts, dict):
        accounts: list[dict[str, Any]] = []
        for host, entries in hosts.items():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict) or not entry.get("login"):
                    continue
                accounts.append(
                    {
                        "login": entry.get("login"),
                        "active": bool(entry.get("active")),
                        "state": entry.get("state"),
                        "host": entry.get("host") or host,
                    }
                )
        return any(a.get("state") == "success" for a in accounts), accounts
    # Older gh (no --json hosts): fall back to the plain status exit code.
    rc, _, _ = _gh(["auth", "status"], cwd=root)
    return rc == 0, []


def _gh_signed_in_to_host(root: str, host: str) -> bool:
    """Whether ``gh`` reports a login on ``host``.

    Some setups sign ``gh`` in to an enterprise host only when a command names that host,
    so a host missing from the global listing is asked about directly.
    """
    _, accounts = _list_accounts(root)
    if host in {account.get("host") for account in accounts}:
        return True
    rc, _, _ = _gh(["auth", "status", "-h", host], cwd=root)
    return rc == 0


def _resolved_base_nwo(root: str) -> str | None:
    """Return the gh-resolved base repo ``owner/repo`` for the checkout, or ``None``.

    Reads ``gh repo set-default --view`` — a local git-config read (no network),
    the same base ``gh`` itself resolves PRs against. ``None`` when no default is
    set (``--view`` exits non-zero) or the output isn't an ``owner/repo``.
    """
    rc, out, _ = _gh(["repo", "set-default", "--view"], cwd=root)
    if rc != 0:
        return None
    value = out.strip()
    if value and "/" in value and " " not in value:
        return value
    return None


def _workspace_key(root: str) -> str | None:
    """Return the stable per-workspace key for the account preference, or ``None``.

    The account preference is keyed by the **main worktree path** — the first
    entry of ``git worktree list`` — so a checkout and all its linked worktrees
    (which share one ``.git``) resolve to the same key, while separate clones stay
    distinct. Purely local (no network, no auth), so it's available before any
    ``gh`` call — the constraint that rules out keying on the resolved base repo.
    """
    rc, out, _ = _git(["worktree", "list", "--porcelain"], cwd=root)
    if rc != 0:
        return None
    for line in out.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree ") :].strip()
            return path or None
    return None


_AUTH_FAILURE_PATTERN = re.compile(
    r"HTTP 401|bad credentials|gh auth login|not logged in|authentication failed"
    r"|token in .* is invalid|requires authentication",
    re.IGNORECASE,
)


def _gh_auth_token(root: str, login: str, host: str = "github.com") -> str | None:
    """Return *login*'s GitHub token via ``gh auth token --user`` (never logged).

    Remembered per host and login while ``hosts.yml`` is unchanged, and until a ``gh``
    call fails to authenticate.
    """

    def fetch() -> str | None:
        rc, out, _ = _gh(["auth", "token", "--user", login, "-h", host], cwd=root)
        return out.strip() or None if rc == 0 else None

    return cached_gh_token(host, login, fetch)


def _forget_account_lookups(token: str | None) -> None:
    """Drop the remembered tokens and account list after an auth failure, so a re-login shows.

    A failed call as a chosen account drops that token; one as ``gh``'s own auth drops all.
    """
    forget_gh_tokens(token)
    with _preview_lock:
        for key in [k for k in _preview_cache if k[:1] == ("accounts",)]:
            del _preview_cache[key]
    with suppress(OSError):
        _cache_path(_accounts_key()).unlink()


def _account_token_for(root: str, workspace_key: str | None = None) -> str | None:
    """Return the GH_TOKEN to run ``gh`` as this workspace's preferred account.

    ``None`` (use ``gh``'s active auth) inside a sandbox (single broker identity),
    when the workspace key can't be resolved, or when it has no stored preference.

    :param root: Absolute workspace path.
    :param workspace_key: Precomputed key to skip a repeat resolution.
    """
    if _in_sandbox():
        return None
    key = workspace_key if workspace_key is not None else _workspace_key(root)
    if not key:
        return None
    login = _config.github_account_preference(key)
    if not login:
        return None
    return _gh_auth_token(root, login)


# Cap the per-check list so a pathological rollup can't bloat the payload; the
# counts stay exact regardless.
_MAX_CHECK_RUNS = 300


def _classify_check(check: dict[str, Any]) -> str:
    """Bucket a single ``statusCheckRollup`` entry by its outcome."""
    # CheckRun carries status/conclusion; StatusContext carries state.
    state = check.get("state")
    if state is not None:
        upper = str(state).upper()
        if upper == "SUCCESS":
            return "passing"
        if upper in ("FAILURE", "ERROR"):
            return "failing"
        return "pending"
    if str(check.get("status", "")).upper() != "COMPLETED":
        return "pending"
    conclusion = str(check.get("conclusion", "")).upper()
    if conclusion in ("CANCELLED", "SKIPPED"):
        return conclusion.lower()
    return "passing" if conclusion in ("SUCCESS", "NEUTRAL") else "failing"


def _summarize_checks(rollup: Any) -> dict[str, Any]:
    """Summarize a ``statusCheckRollup`` into bucket counts + per-check details.

    :returns: Outcome counts, a total, and ``runs`` containing
        ``{name, bucket, url}`` (the job names the UI shows on hover).
    """
    counts = {"passing": 0, "failing": 0, "pending": 0, "cancelled": 0, "skipped": 0}
    runs: list[dict[str, Any]] = []
    if isinstance(rollup, list):
        for check in rollup:
            if not isinstance(check, dict):
                continue
            bucket = _classify_check(check)
            counts[bucket] += 1
            if len(runs) < _MAX_CHECK_RUNS:
                # CheckRun → name (falling back to the workflow); StatusContext
                # → context. Link is detailsUrl (CheckRun) or targetUrl (status).
                name = check.get("name") or check.get("context") or check.get("workflowName")
                runs.append(
                    {
                        "name": str(name) if name else "check",
                        "bucket": bucket,
                        "url": check.get("detailsUrl") or check.get("targetUrl") or None,
                    }
                )
    return {
        **counts,
        "total": sum(counts.values()),
        "runs": runs,
    }


# Cap the comments list so a very chatty PR can't bloat the payload; keep the
# latest comments after merging issue comments and review bodies.
_MAX_COMMENTS = 100


def _shape_reviewers(data: dict[str, Any]) -> list[dict[str, str]]:
    """Combine completed reviews with outstanding requests, including re-requests."""
    reviewers: dict[tuple[str, str], dict[str, str]] = {}
    reviews = data.get("latestReviews")
    for review in reviews if isinstance(reviews, list) else []:
        if not isinstance(review, dict):
            continue
        author = review.get("author")
        login = author.get("login") if isinstance(author, dict) else None
        state = review.get("state")
        if (
            not isinstance(login, str)
            or not login
            or state
            not in (
                "APPROVED",
                "COMMENTED",
                "CHANGES_REQUESTED",
                "DISMISSED",
            )
        ):
            continue
        reviewers[("user", login.lower())] = {"login": login, "kind": "user", "state": state}

    requests = data.get("reviewRequests")
    for request in requests if isinstance(requests, list) else []:
        if not isinstance(request, dict):
            continue
        kind = "team" if request.get("__typename") == "Team" else "user"
        login = (
            request.get("slug") or request.get("name") if kind == "team" else request.get("login")
        )
        if not isinstance(login, str) or not login:
            continue
        # A re-request is pending even if the reviewer previously approved.
        reviewers[(kind, login.lower())] = {"login": login, "kind": kind, "state": "PENDING"}
    return list(reviewers.values())


def _shape_comments(
    raw: Any, reviews: Any = None, *, pr_url: str | None = None, cap: bool = True
) -> list[dict[str, Any]]:
    """Merge PR conversation comments and submitted review bodies by timestamp."""
    shaped: list[dict[str, Any]] = []
    for comment in raw if isinstance(raw, list) else []:
        if not isinstance(comment, dict) or comment.get("isMinimized"):
            continue
        author = comment.get("author")
        shaped.append(
            {
                "author": author.get("login") if isinstance(author, dict) else None,
                "body": str(comment.get("body") or ""),
                "created_at": comment.get("createdAt") or None,
                "url": comment.get("url") or None,
            }
        )
    for review in reviews if isinstance(reviews, list) else []:
        if (
            not isinstance(review, dict)
            or not review.get("submittedAt")
            or not str(review.get("body") or "").strip()
        ):
            continue
        author = review.get("author")
        shaped.append(
            {
                "author": author.get("login") if isinstance(author, dict) else None,
                "body": review["body"],
                "created_at": review.get("submittedAt") or None,
                # gh's review objects have no URL; the PR conversation contains
                # the review body, so link there rather than inventing an anchor.
                "url": pr_url,
            }
        )
    shaped.sort(key=lambda comment: comment["created_at"] or "")
    return shaped[-_MAX_COMMENTS:] if cap else shaped


def _comments_payload(data: dict[str, Any]) -> dict[str, Any]:
    comments = data.get("comments")
    reviews = data.get(_PR_COMMENTS_FIELD)
    shaped = _shape_comments(comments, reviews, pr_url=data.get("url"), cap=False)
    result: dict[str, Any] = {
        "comments": shaped[-_MAX_COMMENTS:],
        **_timeline_payload(data.get("timelineItems")),
    }
    if (
        not isinstance(comments, list)
        or not isinstance(reviews, list)
        or len(comments) >= _MAX_COMMENTS
        or len(reviews) >= _MAX_COMMENTS
        or len(shaped) > _MAX_COMMENTS
    ):
        result["comments_partial"] = True
    return result


def _timeline_payload(raw: Any) -> dict[str, Any]:
    """Shape commit and force-push activity without treating commit dates as push dates."""
    connection = raw if isinstance(raw, dict) else {}
    nodes = connection.get("nodes")
    page = connection.get("pageInfo")
    partial = not isinstance(page, dict) or page.get("hasPreviousPage") is not False
    events: list[dict[str, Any]] = []
    for node in nodes if isinstance(nodes, list) else []:
        if not isinstance(node, dict) or not isinstance(node.get("id"), str) or not node["id"]:
            partial = True
            continue
        if node.get("__typename") == "PullRequestCommit":
            commit = node.get("commit")
            if not isinstance(commit, dict):
                partial = True
                continue
            author = commit.get("author")
            author = author if isinstance(author, dict) else {}
            user = author.get("user")
            user = user if isinstance(user, dict) else {}
            events.append(
                {
                    "id": node["id"],
                    "kind": "commit",
                    "author": user.get("login") or author.get("name"),
                    "created_at": commit.get("committedDate"),
                    "url": node.get("url"),
                    "sha": commit.get("oid"),
                    "message": commit.get("messageHeadline"),
                }
            )
        elif node.get("__typename") == "HeadRefForcePushedEvent":
            actor = node.get("actor")
            actor = actor if isinstance(actor, dict) else {}
            before = node.get("beforeCommit")
            before = before if isinstance(before, dict) else {}
            after = node.get("afterCommit")
            after = after if isinstance(after, dict) else {}
            events.append(
                {
                    "id": node["id"],
                    "kind": "force_push",
                    "author": actor.get("login"),
                    "created_at": node.get("createdAt"),
                    "url": after.get("url"),
                    "sha": after.get("oid"),
                    "previous_sha": before.get("oid"),
                }
            )
    events.sort(key=lambda event: event["created_at"] or "")
    return {"timeline_events": events, "timeline_partial": partial or not isinstance(nodes, list)}


# GitHub label colors are six hex digits; the panel puts them in inline styles.
_LABEL_COLOR = re.compile(r"[0-9a-fA-F]{6}")
# The label picker lists at most this many of the repository's labels.
_MAX_REPO_LABELS = 1000
# Every ``gh`` call of one label, reviewer, or merge request shares this budget,
# under the 20-second waits of the server and the host tunnel.
_PR_EDIT_REQUEST_SECONDS = 15.0
_LABEL_EDIT_TIMED_OUT = "GitHub took too long to update the labels. Refresh to see which applied."
_MERGE_TIMED_OUT = (
    "GitHub took too long to merge the pull request. Refresh to see whether it merged."
)
_DISABLE_AUTO_MERGE_TIMED_OUT = (
    "GitHub took too long to disable auto-merge. Refresh to see whether it is still on."
)
# ``gh pr merge`` method flags in the order the merge picker offers them, keyed
# by the ``gh repo view`` field that allows each.
_MERGE_METHODS = (
    ("merge", "mergeCommitAllowed"),
    ("squash", "squashMergeAllowed"),
    ("rebase", "rebaseMergeAllowed"),
)


def _shape_labels(raw: Any) -> list[dict[str, str | None]]:
    """Shape GitHub labels into ``{name, color, description}``, skipping malformed entries.

    Accepts both ``gh pr view --json labels`` and REST label objects.
    """
    shaped: list[dict[str, str | None]] = []
    for label in raw if isinstance(raw, list) else []:
        name = label.get("name") if isinstance(label, dict) else None
        if not isinstance(name, str) or not name:
            continue
        color = label.get("color")
        description = label.get("description")
        shaped.append(
            {
                "name": name,
                "color": color.lower()
                if isinstance(color, str) and _LABEL_COLOR.fullmatch(color)
                else None,
                "description": description
                if isinstance(description, str) and description
                else None,
            }
        )
    return shaped


def _head_commit_shas(root: str) -> list[str]:
    """Candidate commit SHAs to resolve the PR by identity, most-pushed first.

    ``@{push}`` is the exact ref the branch was pushed to — the pushed tip even
    when a stacking tool renamed the remote branch. ``HEAD`` is the local tip,
    which equals the pushed tip right after such a push when no upstream tracking
    was configured. Order-preserving and deduped; empty on a detached HEAD.
    """
    shas: list[str] = []
    for rev in ("@{push}", "HEAD"):
        rc, out, _ = _git(["rev-parse", "--verify", "--quiet", rev], cwd=root)
        sha = out.strip()
        if rc == 0 and sha and sha not in shas:
            shas.append(sha)
    return shas


def _remote_nwo(root: str, remote: str) -> str | None:
    """``owner/repo`` for a named git remote, or ``None``."""
    rc, url, _ = _git(["remote", "get-url", remote], cwd=root)
    if rc != 0:
        return None
    return _owner_repo_from_url(url.strip())


def _commit_lookup_repo(root: str) -> str | None:
    """The repo the branch was pushed to — where its commit (and PR) live.

    The branch's tracking remote (``branch.<name>.remote``), else ``origin``. One
    repo suffices: ``commits/{sha}/pulls`` resolves the PR across the fork network
    (it reports the PR's own base repo, whatever that is), and a commit that
    wasn't pushed to this repo won't be in the base repo either — so querying more
    repos for the same commit is redundant.
    """
    return _remote_nwo(root, _commit_lookup_remote(root))


def _commit_lookup_remote(root: str) -> str:
    """The branch's tracking remote if it names a repo, else ``origin``."""
    rc, br, _ = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=root)
    branch = br.strip()
    if rc == 0 and branch and branch != "HEAD":
        rc, remote, _ = _git(["config", f"branch.{branch}.remote"], cwd=root)
        remote = remote.strip()
        if rc == 0 and remote and remote != "." and _remote_nwo(root, remote):
            return remote
    return "origin"


def _remotes_by_repo(root: str, lookup_remote: str) -> dict[str, list[str]]:
    """Remotes keyed by lower-cased ``owner/repo`` on the host of ``lookup_remote``."""
    rc, out, _ = _git(["remote"], cwd=root)
    urls: dict[str, str] = {}
    for remote in out.split() if rc == 0 else []:
        rc, url, _ = _git(["remote", "get-url", remote], cwd=root)
        if rc == 0:
            urls[remote] = url.strip()
    if lookup_remote not in urls:
        return {}
    # A same-named repo on another host, such as a GitLab mirror, is a different repo.
    lookup_host = host_of(urls[lookup_remote])
    by_repo: dict[str, list[str]] = {}
    for remote, url in urls.items():
        nwo = _owner_repo_from_url(url)
        if nwo and host_of(url) == lookup_host:
            by_repo.setdefault(nwo.lower(), []).append(remote)
    return by_repo


def _contained_in_base_branch(root: str, sha: str, remotes: Sequence[str], base_ref: str) -> bool:
    """Whether the local tracking ref of ``base_ref`` on any of ``remotes`` contains ``sha``."""
    for remote in remotes:
        # An exact ref lookup, so a lookalike branch or tag can't stand in for a missing one.
        tracking_ref = f"refs/remotes/{remote}/{base_ref}"
        rc, tracking_oid, _ = _git(["show-ref", "--verify", "--hash", tracking_ref], cwd=root)
        if rc != 0:
            continue
        rc, _, _ = _git(["merge-base", "--is-ancestor", sha, tracking_oid.strip()], cwd=root)
        if rc == 0:
            return True
    return False


def _resolve_pr_via_commit(root: str, *, token: str | None = None) -> tuple[int, str] | None:
    """Resolve ``(PR number, base owner/repo)`` by pushed-commit identity, or ``None``.

    Branch-name-independent: asks GitHub which PR a pushed commit belongs to
    (``repos/{owner}/{repo}/commits/{sha}/pulls``), so it finds the PR even when a
    stacking tool (git-stack, ``git pp``) pushed under a remote branch name that
    differs from the checkout, or a fork PR whose head ``gh`` can't name. The
    commit lives in the repo it was pushed to (the fork for a fork PR), and the
    response names the PR's own base repo — returned so the caller fetches the PR
    from the right place regardless of the local ``gh repo set-default``.

    Only **open** PRs are accepted. For a commit already on the default branch
    (master/main, or any merged tip) the endpoint returns the *merged* PR that
    introduced it, which is a false positive — never the branch's own outgoing
    PR — so a closed/merged row is skipped rather than surfaced. Open rows are
    false positives too when the queried fork's default branch lags the base:
    GitHub then lists every open PR that merges the base past the commit. So an
    open row is skipped when the commit is already on that PR's base branch
    locally (:func:`_contained_in_base_branch`), read from remotes of the base repo
    on the host of the queried fork's remote.
    """
    shas = _head_commit_shas(root)
    repo = _commit_lookup_repo(root)
    if not shas or not repo:
        return None
    base_remotes: dict[str, list[str]] | None = None
    on_base_branch: dict[tuple[str, str, str], bool] = {}

    def _is_candidate(row: Any, sha: str) -> bool:
        nonlocal base_remotes
        if not isinstance(row, dict) or str(row.get("state", "")).lower() != "open":
            return False
        base = row.get("base")
        base_ref = base.get("ref") if isinstance(base, dict) else None
        base_repo = base.get("repo") if isinstance(base, dict) else None
        base_full = base_repo.get("full_name") if isinstance(base_repo, dict) else None
        if not isinstance(base_ref, str) or not isinstance(base_full, str):
            return True
        key = (sha, base_full.lower(), base_ref)
        if key not in on_base_branch:
            if base_remotes is None:
                base_remotes = _remotes_by_repo(root, _commit_lookup_remote(root))
            remotes = base_remotes.get(base_full.lower(), [])
            on_base_branch[key] = _contained_in_base_branch(root, sha, remotes, base_ref)
        return not on_base_branch[key]

    for sha in shas:
        rc, out, _ = _gh(["api", f"repos/{repo}/commits/{sha}/pulls"], cwd=root, token=token)
        if rc != 0:
            continue
        try:
            rows = json.loads(out)
        except ValueError:
            continue
        if not isinstance(rows, list) or not rows:
            continue

        # Accept only an OPEN PR that adds the commit beyond its base branch. Merged
        # rows, and others' PRs listed because the fork's default branch lags, aren't
        # this branch's own PR; skip them rather than falling back to the first row.
        chosen = next((r for r in rows if _is_candidate(r, sha)), None)
        if chosen is None:
            continue
        number = chosen.get("number")
        base = chosen.get("base")
        base_repo = base.get("repo") if isinstance(base, dict) else None
        base_full = base_repo.get("full_name") if isinstance(base_repo, dict) else None
        if isinstance(number, int) and isinstance(base_full, str) and "/" in base_full:
            return number, base_full
    return None


def _refused_pr_fields(stderr: str) -> set[str]:
    """Read rejected JSON fields or GraphQL error paths, excluding CLI help catalogs."""
    if match := re.search(r'^Unknown JSON field: "([^"]+)"', stderr, re.MULTILINE):
        return {match[1]}
    return {
        field
        for path in re.findall(r"\(([\w.]+(?:,\s*[\w.]+)*)\)", stderr)
        for field in re.findall(r"\w+", path)
    }


def _gh_pr_read(
    verb: list[str], fields: str, *, cwd: str, token: str | None = None
) -> tuple[Any, str] | None:
    """Return ``gh pr <verb> --json <fields>`` parsed, with the fields it answered, or ``None``.

    Retry without optional checks, reviewer, or merge fields only when the error
    names them. Each retry removes fields, so unrelated failures never retry here.
    """
    rc, out, err = _gh(["pr", *verb, "--json", fields], cwd=cwd, token=token)
    while rc != 0:
        if _note_rate_limit(err, cwd=cwd, token=token):
            return None
        refused = _refused_pr_fields(err) & set(fields.split(","))
        refused &= _PR_REVIEW_FIELDS | _PR_MERGE_FIELDS | {_PR_CHECKS_FIELD, _PR_COMMENTS_FIELD}
        if refused & _PR_REVIEW_FIELDS:
            refused.update(_PR_REVIEW_FIELDS)
        remaining = ",".join(field for field in fields.split(",") if field not in refused)
        if not remaining or remaining == fields:
            return None
        fields = remaining
        rc, out, err = _gh(["pr", *verb, "--json", fields], cwd=cwd, token=token)
    try:
        return json.loads(out), fields
    except ValueError:
        return None


def _gh_pr_view(
    args: list[str], fields: str, *, cwd: str, token: str | None = None
) -> dict[str, Any] | None:
    """Return ``gh pr view <args> --json <fields>`` as a dict, or ``None``."""
    read = _gh_pr_read(["view", *args], fields, cwd=cwd, token=token)
    if read is None or not isinstance(read[0], dict):
        return None
    data, answered = read
    if "comments" in answered.split(","):
        _refresh_conversation(data, cwd=cwd, token=token)
    return data


def _refresh_conversation(data: dict[str, Any], *, cwd: str, token: str | None) -> None:
    """Read recent comments and activity, preserving gh's comments if this read fails."""
    url = data.get("url")
    if not isinstance(url, str):
        return
    try:
        reference = PullRequestRef.from_url(url)
        host = _host_args(cwd, reference)
    except ValueError:
        return
    comments = data.get("comments")
    refresh_comments = isinstance(comments, list) and len(comments) >= _MAX_COMMENTS
    owner, name = reference.repository.split("/", 1)
    query = """query($owner: String!, $name: String!, $number: Int!, $includeComments: Boolean!) {
      repository(owner: $owner, name: $name) {
        pullRequest(number: $number) {
          comments(last: 100) @include(if: $includeComments) {
            nodes { author { login } body createdAt url isMinimized }
          }
          timelineItems(last: 100, itemTypes: [PULL_REQUEST_COMMIT, HEAD_REF_FORCE_PUSHED_EVENT]) {
            pageInfo { hasPreviousPage }
            nodes {
              __typename
              ... on PullRequestCommit {
                id url
                commit { oid messageHeadline committedDate author { name user { login } } }
              }
              ... on HeadRefForcePushedEvent {
                id createdAt actor { login }
                beforeCommit { oid }
                afterCommit { oid url }
              }
            }
          }
        }
      }
    }"""
    rc, out, err = _gh(
        [
            "api",
            *host,
            "graphql",
            # ponytail: gh handles token-scoped caching; busy comment tails stay fresh.
            *([] if refresh_comments else ["--cache", "30s"]),
            "-f",
            f"query={query}",
            "-f",
            f"owner={owner}",
            "-f",
            f"name={name}",
            "-F",
            f"number={reference.number}",
            "-F",
            f"includeComments={str(refresh_comments).lower()}",
        ],
        cwd=cwd,
        token=token,
    )
    if rc != 0:
        _note_rate_limit(err, cwd=cwd, token=token)
        return
    try:
        conversation = json.loads(out)["data"]["repository"]["pullRequest"]
    except (ValueError, KeyError, TypeError):
        return
    if not isinstance(conversation, dict):
        return
    data["timelineItems"] = conversation.get("timelineItems")
    comments = conversation.get("comments")
    latest = comments.get("nodes") if isinstance(comments, dict) else None
    if isinstance(latest, list):
        data["comments"] = latest


def _checks_fields(data: dict[str, Any]) -> dict[str, Any]:
    """``checks`` and ``checks_supported`` for a ``gh pr view`` object; the field is
    absent only after :func:`_gh_pr_view` dropped it."""
    return {
        "checks": _summarize_checks(data.get(_PR_CHECKS_FIELD)),
        "checks_supported": _PR_CHECKS_FIELD in data,
    }


def _merge_fields(data: dict[str, Any]) -> dict[str, Any]:
    """``merge_state_status`` and ``auto_merge`` for a ``gh pr view`` object.

    Each is left out when :func:`_gh_pr_view` dropped its field for an older ``gh``.
    """
    fields: dict[str, Any] = {}
    if "mergeStateStatus" in data:
        status = data["mergeStateStatus"]
        fields["merge_state_status"] = status.upper() if isinstance(status, str) else None
    if "autoMergeRequest" in data:
        request = data["autoMergeRequest"]
        if isinstance(request, dict):
            method = request.get("mergeMethod")
            enabled_by = request.get("enabledBy")
            login = enabled_by.get("login") if isinstance(enabled_by, dict) else None
            fields["auto_merge"] = {
                "merge_method": method.upper() if isinstance(method, str) else None,
                "enabled_by": login if isinstance(login, str) else None,
                "enabled_at": request.get("enabledAt"),
            }
        else:
            fields["auto_merge"] = None
    return fields


def _pr_view_json(root: str, fields: str, *, token: str | None = None) -> dict[str, Any] | None:
    """Return the branch's PR as a ``gh``-JSON object for ``fields``, or ``None``.

    Two-step, so a decoupled remote branch name never hides the PR:

    1. ``gh pr view --json`` — resolves the current branch's PR against the
       gh-resolved base as the authenticated account. Fast, and covers same-repo
       branches and any push that set upstream tracking (plain ``push -u``, and
       ``git pp``, which pushes ``-u``). ``--json`` also avoids the interactive
       pager and the Projects-classic mis-parse of a bare ``gh pr view``.
    2. Commit-identity fallback — when a stacking tool (git-stack, ``git pp``)
       pushed under a remote branch name that differs from the checkout, or a
       fork PR whose head ``gh`` can't name, the branch-name lookup misses. The
       pushed commit still maps to the PR (:func:`_resolve_pr_via_commit`), which
       also yields the PR's base repo so the full object is fetched with an
       explicit ``-R``. This subsumes the old ``branch.<name>.merge`` head guess.

    :param token: Optional GH_TOKEN to run the calls as the selected account.
    """
    data = _gh_pr_view([], fields, cwd=root, token=token)
    if data is not None:
        return data

    resolved = _resolve_pr_via_commit(root, token=token)
    if resolved is None:
        return None
    number, base_repo = resolved
    # First-run convenience: point gh's default at the PR's real base so the
    # agent's own `gh` in the terminal targets it too. Only when unset, so a
    # manual `gh repo set-default` is never overridden; best-effort — resolution
    # itself doesn't need it (the explicit ``-R`` below carries the base).
    if not _resolved_base_nwo(root):
        _gh(["repo", "set-default", base_repo], cwd=root, token=token)
    return _gh_pr_view([str(number), "-R", base_repo], fields, cwd=root, token=token)


def _repo_payload(name_with_owner: str | None, url: object) -> dict[str, Any]:
    """The repo payload; the forge host (omitted when unknown) tells github.com from Enterprise."""
    repo: dict[str, Any] = {"name_with_owner": name_with_owner}
    host = host_of(url) if isinstance(url, str) else None
    if host:
        repo["host"] = host
    return repo


def _workspace_github_info(root: str) -> dict[str, Any]:
    """Resolve GitHub context for the workspace: repo, branch, base, and PR.

    Git-first: a git checkout is the fundamental requirement, so ``available``
    reflects "is a git repo". ``gh`` layers the repo / PR metadata on top;
    ``base_ref`` is the PR's base branch (``None`` when there's no PR, since the
    tab is a pure PR view).

    :param root: Absolute path to the session workspace.
    :returns: A ``session.github.info`` object. ``available`` is false only when
        this isn't a git repo (``reason: not_a_git_repo``). ``gh_available`` /
        ``authenticated`` report whether the ``gh`` CLI is present and signed in;
        ``repo`` / ``pr`` / ``base_ref`` are null without it. ``accounts`` /
        ``selected_account`` are populated only when the repo can't be reached (to
        drive the account selector), so the happy path stays fast. The GitHub
        facet copies these four fields into ``auth``; at the top level they are
        deprecated and will be removed in 0.19.0.

    Ordering is a fast-path optimization: resolve the PR first, and only fall to
    the ``gh repo view`` reachability probe (and, if that fails, the ``gh auth
    status`` account enumeration) when there's no PR. A resolved PR already tells
    us the repo and that we're authenticated, so those extra network calls are
    skipped whenever a PR exists.
    """
    payload: dict[str, Any] = {"object": "session.github.info"}

    rc, out, _ = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=root)
    if rc != 0:
        payload.update(available=False, reason="not_a_git_repo")
        return payload
    branch = out.strip()
    payload.update(
        available=True,
        branch=branch,
        base_ref=None,
        repo=None,
        pr=None,
    )

    # gh is an enhancement layer: without it (or its auth) the git diff still
    # renders; the UI notes the missing CLI / sign-in from these flags.
    if shutil.which("gh") is None:
        payload.update(gh_available=False, authenticated=False)
        return payload
    payload["gh_available"] = True

    # Run the API-touching calls as the workspace's preferred account (local dev);
    # resolving the token is local (config read + keyring), so it's always cheap.
    token = _account_token_for(root)

    # Resolve the PR first. A hit tells us the repo (from the PR URL) and that
    # we're authenticated, so we skip the separate `gh repo view` and the account
    # enumeration entirely — the common case makes the fewest network calls.
    data = _pr_view_json(root, _PR_VIEW_FIELDS, token=token)
    if data is not None:
        author = data.get("author")
        body = data.get("body")
        payload["pr"] = {
            "number": data.get("number"),
            "title": data.get("title"),
            "state": data.get("state"),
            "url": data.get("url"),
            "is_draft": data.get("isDraft", False),
            "author": author.get("login") if isinstance(author, dict) else None,
            "base_ref": data.get("baseRefName"),
            "head_ref": data.get("headRefName"),
            **_checks_fields(data),
            # An empty body is null so the Summary tab shows its empty state.
            "body": body if isinstance(body, str) and body.strip() else None,
            **_comments_payload(data),
            **_merge_fields(data),
        }
        if _PR_REVIEW_FIELDS.issubset(data):
            payload["pr"]["reviewers"] = _shape_reviewers(data)
        if "labels" in data:
            payload["pr"]["labels"] = _shape_labels(data["labels"])
        payload["authenticated"] = True
        payload["base_ref"] = data.get("baseRefName")
        payload["repo"] = _repo_payload(_owner_repo_from_pr_url(data.get("url")), data.get("url"))
        if payload["repo"]["name_with_owner"]:
            payload["workspace_repo"] = dict(payload["repo"])
        return payload

    # No PR. Probe repo reachability to tell "no PR yet" from "can't reach repo".
    rc, out, _ = _gh(["repo", "view", "--json", "nameWithOwner,url"], cwd=root, token=token)
    if rc == 0:
        try:
            view = json.loads(out)
            nwo, url = view.get("nameWithOwner"), view.get("url")
        except (ValueError, AttributeError):
            nwo = url = None
        if nwo:
            payload["authenticated"] = True
            payload["repo"] = _repo_payload(nwo, url)
            payload["workspace_repo"] = dict(payload["repo"])
            return payload  # -> no-pr

    # Repo unreachable / not signed in — enumerate accounts so the panel can offer
    # the account selector (the only state where it's shown).
    authenticated, accounts = _cached_accounts(root)
    payload["authenticated"] = authenticated
    payload["accounts"] = accounts
    workspace_key = _workspace_key(root)
    pref_login = _config.github_account_preference(workspace_key) if workspace_key else None
    active_login = next((a["login"] for a in accounts if a.get("active")), None)
    payload["selected_account"] = pref_login or active_login
    return payload


def _repo_argument(ref: PullRequestRef) -> str:
    """The ``gh -R`` selector, including a GitHub Enterprise host.

    It is also the key of saved per-PR account preferences, so its format is fixed.
    """
    if ref.provider != "github":
        raise ValueError("This resource supports only GitHub pull requests")
    return f"{ref.host}/{ref.repository}"


def _repo_token(root: str, repo_argument: str) -> str | None:
    """The token of the account preferred for ``[host/]owner/repo``, or ``None``."""
    if _in_sandbox():
        return None
    login = _config.github_account_preference(repo_argument)
    if login:
        host = repo_argument.split("/")[0] if repo_argument.count("/") > 1 else "github.com"
        return _gh_auth_token(root, login, host)
    return None


def _pr_token(root: str, reference: PullRequestRef) -> str | None:
    return _repo_token(root, _repo_argument(reference))


_DERIVE_TOKEN: Any = object()


def _pr_json(
    root: str, reference: PullRequestRef, fields: str, token: Any = _DERIVE_TOKEN
) -> dict[str, Any] | None:
    """``gh pr view`` of ``reference``, as ``token`` or, by default, its preferred account."""
    token = _pr_token(root, reference) if token is _DERIVE_TOKEN else token
    if not _gh_can_read_host(root, reference.host, token):
        return None
    return _gh_pr_view(
        [str(reference.number), "-R", _repo_argument(reference)], fields, cwd=root, token=token
    )


_REFERENCE_EXTRA_FIELDS = ",headRefOid,baseRefOid,headRepository,headRepositoryOwner"
# A background refresh stores only state, draft and title, so it reads no nested lists
# (checks, reviews, labels, comments), which make up most of a listing's GraphQL cost.
_LISTING_FIELDS = (
    "number,title,state,url,isDraft,author,baseRefName,headRefName" + _REFERENCE_EXTRA_FIELDS
)
_LISTING_LIMIT = 100
# A viewer sees CI and reviews change within a minute, so open tabs don't drain the token's
# quota. A merged PR doesn't change back; a closed one can be reopened, so only the
# server's refresher skips re-reading it.
_OPEN_PR_TTL_SECONDS = 60.0
# Background refreshes of one wave share a read; the server refreshes a PR every 300 s.
_BACKGROUND_PR_TTL_SECONDS = 120.0
_FINISHED_PR_TTL_SECONDS = 6 * 3600.0
_ACCOUNTS_TTL_SECONDS = 30.0
# A failed read is remembered briefly so waiters on one lock don't each retry ``gh``.
_FAILED_READ_TTL_SECONDS = 5.0

# Set for the server's own refreshes, which need no conversation (comments, timeline).
_background_read: ContextVar[bool] = ContextVar("github_background_read", default=False)


@contextmanager
def background_reads(enabled: object) -> Iterator[None]:
    """Mark PR reads inside the block as background refreshes (``"1"`` in a query string)."""
    token = _background_read.set(enabled is True or enabled in ("1", "true"))
    try:
        yield
    finally:
        _background_read.reset(token)


# One stamp per host: every runner skips background PR reads until GitHub's quota resets.
_RATE_LIMIT_KEY = ("rate-limited",)
# GitHub's own wording, for the primary quota and for its secondary (abuse) limits.
_RATE_LIMIT_PATTERN = re.compile(
    r"rate limit (?:already )?exceeded|secondary rate limit", re.IGNORECASE
)
_RATE_LIMIT_DEFAULT_SECONDS = 15 * 60.0
_RATE_LIMIT_MAX_SECONDS = 3600.0


def _rate_limit_reset(stderr: str, *, cwd: str, token: str | None) -> float:
    """When the exhausted quota resets, in epoch seconds; 15 minutes on when ``gh`` can't say.

    ``gh api rate_limit`` doesn't count against the quota it reports.
    """
    now = time.time()
    resource = "graphql" if "graphql" in stderr.lower() else "core"
    rc, out, _ = _gh(["api", "rate_limit"], cwd=cwd, token=token)
    try:
        quota = json.loads(out)["resources"][resource]
        reset = float(quota["reset"]) if rc == 0 and int(quota["remaining"]) == 0 else 0.0
    except (ValueError, KeyError, TypeError):
        reset = 0.0
    if reset <= now:
        return now + _RATE_LIMIT_DEFAULT_SECONDS
    return min(reset, now + _RATE_LIMIT_MAX_SECONDS)


def _note_rate_limit(stderr: str, *, cwd: str, token: str | None) -> bool:
    """Stamp the host as rate limited when ``stderr`` says so; whether it did."""
    if not _RATE_LIMIT_PATTERN.search(stderr):
        return False
    until = _rate_limit_reset(stderr, cwd=cwd, token=token)
    _logger.warning("github_resource: GitHub rate limit hit; pausing background PR reads")
    _cache_put(_RATE_LIMIT_KEY, until, until - time.time(), shared=True)
    return True


def _background_reads_paused() -> bool:
    """Whether this is a background read while the host's rate-limit stamp holds."""
    return _background_read.get() and _cache_get(_RATE_LIMIT_KEY, shared=True)[0]


def _accounts_key() -> tuple[str, ...]:
    """The accounts cache key: the config dir and a hash of the environment's gh tokens.

    Without ``hosts.yml`` the accounts come from those tokens, so runners holding
    different ones must not share an entry.
    """
    names = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")
    tokens = json.dumps([os.environ.get(name, "") for name in names])
    return ("accounts", gh_config_dir(), hashlib.sha256(tokens.encode()).hexdigest())


def _cached_accounts(root: str) -> tuple[bool, list[dict[str, Any]]]:
    """:func:`_list_accounts`, shared by the host's runners while ``hosts.yml`` is unchanged.

    An entry is used only inside its window and while the accounts file still has the
    stat stored with it, so a switch is seen at once. The window still bounds a token
    that expires without the file changing. Concurrent callers, in any of the host's
    runners, share one lookup per window.
    """
    config_dir = Path(gh_config_dir())
    key = _accounts_key()
    stat = _hosts_signature(config_dir)

    def cached() -> tuple[bool, list[dict[str, Any]]] | None:
        hit, entry = _cache_get(key, shared=True)
        if hit and isinstance(entry, dict) and entry.get("stat") == stat:
            return bool(entry.get("authenticated")), list(entry.get("accounts") or [])
        return None

    if (answer := cached()) is not None:
        return answer
    with _single_flight(key):
        if (answer := cached()) is not None:
            return answer
        authenticated, accounts = _list_accounts(root)
        # A failed lookup (offline, timeout) is retried soon; only a real answer is kept long.
        ttl = _ACCOUNTS_TTL_SECONDS if accounts or authenticated else _FAILED_READ_TTL_SECONDS
        _cache_put(
            key,
            {"authenticated": authenticated, "accounts": accounts, "stat": stat},
            ttl,
            shared=True,
        )
        return authenticated, accounts


def _hosts_signature(config_dir: Path) -> list[int]:
    """``hosts.yml``'s stat, or ``[]`` when it is missing (auth then comes from the environment).

    Creating the file changes the signature, so a login is still seen at once.
    """
    return hosts_file_stat(config_dir) or []


# Hosts ``gh`` reads GH_TOKEN for: github.com and github.localhost with their subdomains
# (``gh`` canonicalizes those), and the ``*.ghe.com`` tenancy hosts.
_PUBLIC_TOKEN_HOSTS = ("github.com", "github.localhost")
_PUBLIC_TOKEN_HOST_SUFFIXES = (".github.com", ".github.localhost", ".ghe.com")


def _ambient_token(host: str) -> str | None:
    """The token ``gh`` takes from the environment for *host*, by its own host rules.

    github.com, ``github.localhost`` (and any subdomain of either, which ``gh``
    canonicalizes) and ``*.ghe.com`` read ``GH_TOKEN`` then ``GITHUB_TOKEN``; any other
    (Enterprise Server) host reads ``GH_ENTERPRISE_TOKEN`` then ``GITHUB_ENTERPRISE_TOKEN``.
    A sandbox scrubs them all.
    """
    if _in_sandbox():
        return None
    hostname = host.lower()
    names = (
        ("GH_TOKEN", "GITHUB_TOKEN")
        if hostname in _PUBLIC_TOKEN_HOSTS or hostname.endswith(_PUBLIC_TOKEN_HOST_SUFFIXES)
        else ("GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")
    )
    return next((os.environ[name] for name in names if os.environ.get(name)), None)


# Hosts whose active account had no token, per config dir: the ``hosts.yml`` stat and
# when to ask again, so a host without a login isn't asked on every read.
_missing_active_tokens: dict[tuple[str, str], tuple[list[int] | None, float]] = {}
_missing_active_tokens_lock = threading.Lock()


def _active_gh_token(root: str, host: str) -> str | None:
    """The token of ``gh``'s active account for *host*, from ``gh auth token`` (no API call).

    Remembered while ``hosts.yml`` keeps its stat, and dropped when a ``gh`` call fails to
    authenticate. A host with no token is asked again after the accounts window, or at
    once when ``hosts.yml`` changes.
    """
    config_dir = gh_config_dir()
    stat = hosts_file_stat(config_dir)
    miss_key = (config_dir, host)
    with _missing_active_tokens_lock:
        miss = _missing_active_tokens.get(miss_key)
    if miss is not None and miss[0] == stat and miss[1] > time.monotonic():
        return None
    answered = False

    def fetch() -> str | None:
        nonlocal answered
        rc, out, _ = _gh(["auth", "token", "--hostname", host], cwd=root)
        answered = rc is not None  # a spawn failure or deadline is no answer
        return out.strip() or None if rc == 0 else None

    token = cached_gh_token(host, None, fetch)
    if token is None and answered:
        with _missing_active_tokens_lock:
            _missing_active_tokens[miss_key] = (stat, time.monotonic() + _ACCOUNTS_TTL_SECONDS)
    return token


def _resolve_read(root: str, token: str | None, host: str) -> tuple[str | None, str | None]:
    """Decide once who a read is made as: ``(token, cache_identity)``.

    The read and its cache key both come from this, so they cannot disagree. ``token``,
    the preferred account's (``None`` when it has none readable), is passed to ``gh`` as
    given and keyed by its hash. Without one ``gh`` uses the environment token for the
    host (see :func:`_ambient_token`), keyed by its hash, and otherwise its active
    account. That account's own token is then passed explicitly, so a ``gh auth switch``
    between this and the read cannot change who answers. A preferred account whose token
    can't be read falls through to those, as ``gh`` does. No call checks the token: the
    read itself shows whether it works. When no credential can be bound the identity is
    ``None``.
    """
    if token:
        return token, "token:" + hashlib.sha256(token.encode()).hexdigest()
    ambient = _ambient_token(host)
    if ambient:
        return None, "env:" + hashlib.sha256(ambient.encode()).hexdigest()
    active_token = _active_gh_token(root, host)
    if not active_token:
        return None, None
    return active_token, "token:" + hashlib.sha256(active_token.encode()).hexdigest()


def _gh_can_read_host(root: str, host: str, token: str | None) -> bool:
    """Whether ``gh`` has a credential for *host*: github.com, or a token it would use there."""
    return (
        host == "github.com"
        or bool(token or _ambient_token(host) or _active_gh_token(root, host))
        # Some setups sign gh in to an enterprise host only when a command names it.
        or _gh_signed_in_to_host(root, host)
    )


def _unbound_identity(host: str) -> str:
    """Cache identity for a read no account was bound to: the config dir, its ``hosts.yml``
    stat and the environment token's hash, so one login's or token's reads never serve another.
    """
    config_dir = Path(gh_config_dir())
    ambient = _ambient_token(host) or ""
    material = json.dumps([str(config_dir), _hosts_signature(config_dir), ambient])
    return "unbound:" + hashlib.sha256(material.encode()).hexdigest()


def _pr_read(root: str, reference: PullRequestRef) -> tuple[str | None, str | None]:
    """``(token, cache_identity)`` for a PR read; see :func:`_resolve_read`."""
    return _resolve_read(root, _pr_token(root, reference), reference.host)


@contextmanager
def _single_flight(key: tuple[str, ...]) -> Iterator[None]:
    """Let one process at a time fill ``key``; the others wait, then find it cached."""
    descriptor = None
    try:
        lock_path = _cache_path(key).with_suffix(".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        if fcntl is not None:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
    except OSError:
        _logger.debug("github_resource: could not lock a cached read", exc_info=True)
    try:
        yield
    finally:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)


def _open_pr_listing(
    root: str, reference: PullRequestRef, identity: str, token: str | None
) -> list[dict[str, Any]] | None:
    """The repository's open PRs from one ``gh pr list``, shared by every session reading it."""
    key = ("pr-list", identity, _repo_argument(reference).lower())
    hit, listing = _cache_get(key, shared=True)
    if hit:
        return listing
    with _single_flight(key):
        hit, listing = _cache_get(key, shared=True)
        if hit:
            return listing
        started = time.time()
        read = _gh_pr_read(
            [
                "list",
                "-R",
                _repo_argument(reference),
                "--state",
                "open",
                "--limit",
                str(_LISTING_LIMIT),
            ],
            _LISTING_FIELDS,
            cwd=root,
            token=token,
        )
        if read is None or not isinstance(read[0], list):
            _cache_put(key, None, _FAILED_READ_TTL_SECONDS, shared=True, started=started)
            return None
        _cache_put(key, read[0], _BACKGROUND_PR_TTL_SECONDS, shared=True, started=started)
        return read[0]


def _reference_pr_data(
    root: str, reference: PullRequestRef, *, background: bool
) -> dict[str, Any] | None:
    """One tracked PR's ``gh`` data for the panel, shared across sessions and refreshes.

    A background refresh reads open PRs from the repository's shared listing, which
    has no conversation. A viewer reads the PR itself, cached for the poll window.
    A merged PR is cached for long, as is a closed one a background refresh read.
    """
    fields = _LISTING_FIELDS if background else _PR_VIEW_FIELDS + _REFERENCE_EXTRA_FIELDS
    token, identity = _pr_read(root, reference)
    unbound = identity is None
    if identity is None:
        identity = _unbound_identity(reference.host)
    key = (
        "pr",
        identity,
        _repo_argument(reference).lower(),
        str(reference.number),
        "background" if background else "full",
    )
    hit, data = _cache_get(key, shared=True)
    if hit:
        return data
    with _single_flight(key):
        hit, data = _cache_get(key, shared=True)
        if hit:
            return data
        if background and reference.host == "github.com" and not unbound:
            listing = _open_pr_listing(root, reference, identity, token)
            for entry in listing or []:
                if isinstance(entry, dict) and entry.get("number") == reference.number:
                    return entry
            if _background_reads_paused():
                return None
        started = time.time()
        data = _pr_json(root, reference, fields, token)
        if data is None:
            if not unbound:
                _cache_put(key, None, _FAILED_READ_TTL_SECONDS, shared=True, started=started)
            return None
        state = data.get("state")
        finished = state == "MERGED" or (background and state == "CLOSED")
        if unbound and not finished:
            return data  # no known identity: only a finished PR's read is shared
        if finished:
            ttl = _FINISHED_PR_TTL_SECONDS
        else:
            ttl = _BACKGROUND_PR_TTL_SECONDS if background else _OPEN_PR_TTL_SECONDS
        _cache_put(key, data, ttl, shared=True, started=started)
        return data


def _scope_of(key: Sequence[str]) -> tuple[str, str | None] | None:
    """``(repository, number)`` a PR cache key covers (``None`` number: the listing)."""
    if key[0] == "pr":
        return key[2], key[3]
    if key[0] == "pr-list":
        return key[2], None
    return None


def _invalidation_directory() -> Path:
    return _cache_directory() / "invalidated"


def _invalidated_since(key: Sequence[str]) -> float:
    """When a write last invalidated ``key``'s PR (0 when none did), as other runners see it."""
    scope = _scope_of(key)
    latest = 0.0
    if scope is None:
        return latest
    for path in _invalidation_directory().glob("*.json"):
        try:
            marker = json.loads(path.read_text(encoding="utf-8"))
            repository, number, at = marker["repository"], marker["number"], float(marker["at"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if repository in ("*", scope[0]) and (number == "*" or scope[1] in (None, number)):
            latest = max(latest, at)
    return latest


def _record_invalidation(repository: str, number: str) -> None:
    """Stamp the PR (``*`` for all) as written now, so reads begun earlier aren't cached."""
    directory = _invalidation_directory()
    digest = hashlib.sha256(f"{repository}\0{number}".encode()).hexdigest()
    stamp = directory / f"{digest}.json"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        try:
            earlier = float(json.loads(stamp.read_text(encoding="utf-8"))["at"])
        except (OSError, ValueError, KeyError, TypeError):
            earlier = 0.0
        descriptor, temporary = tempfile.mkstemp(dir=directory, suffix=".tmp")
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(
                {"repository": repository, "number": number, "at": max(earlier, time.time())},
                handle,
            )
        os.replace(temporary, stamp)
    except OSError:
        _logger.debug("github_resource: could not record an invalidation", exc_info=True)


def invalidate_pr_reads(pr_url: str | None) -> None:
    """Drop the shared PR entries a write made stale: the PR's, and its repository's listing.

    Without a ``pr_url`` (a write to the checkout's own PR) every PR entry goes. The write
    is also stamped on disk, so a read already in flight is not cached when it returns.
    """
    scope: tuple[str, str] | None = None
    if pr_url:
        try:
            reference = PullRequestRef.from_url(pr_url)
            scope = (_repo_argument(reference).lower(), str(reference.number))
        except ValueError:
            pass
    _record_invalidation(*(scope or ("*", "*")))

    def stale(key: Sequence[str]) -> bool:
        if key[0] == "pr":
            return scope is None or tuple(key[2:4]) == scope
        return key[0] == "pr-list" and (scope is None or key[2] == scope[0])

    with _preview_lock:
        for key in [key for key in _preview_cache if stale(key)]:
            del _preview_cache[key]
        for key in [key for key in _finished_titles if stale(key)]:
            del _finished_titles[key]
    for path in _cache_directory().glob("*.json"):
        try:
            key = json.loads(path.read_text(encoding="utf-8")).get("key")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(key, list) and key and stale(key):
            with suppress(OSError):
                path.unlink()


def _head_repository_url(reference: PullRequestRef, head: object, owner: object) -> str | None:
    """The source (possibly fork) repository's web URL; ``None`` once the fork is deleted."""
    # Older gh reports headRepository as only {id, name}, so the owner comes separately.
    name = head.get("name") if isinstance(head, dict) else None
    login = owner.get("login") if isinstance(owner, dict) else None
    if isinstance(name, str) and name and isinstance(login, str) and login:
        return f"https://{reference.host}/{login}/{name}"
    return None


def _reference_info(root: str, reference: PullRequestRef) -> dict[str, Any]:
    info: dict[str, Any] = {
        "object": "session.github.info",
        "available": True,
        "gh_available": shutil.which("gh") is not None,
        "authenticated": False,
        "pr": None,
        "repo": {"name_with_owner": reference.repository, "host": reference.host},
        "selected_pr_url": reference.url,
    }
    if not info["gh_available"] or _background_reads_paused():
        return info
    data = _reference_pr_data(root, reference, background=_background_read.get())
    _, accounts = _cached_accounts(root)
    info["accounts"] = [a for a in accounts if a.get("host") == reference.host]
    preferred = _config.github_account_preference(_repo_argument(reference))
    info["selected_account"] = preferred or next(
        (a["login"] for a in info["accounts"] if a.get("active")), None
    )
    if data is None:
        return info
    author = data.get("author")
    info.update(
        authenticated=True, branch=data.get("headRefName"), base_ref=data.get("baseRefName")
    )
    info["pr"] = {
        "number": reference.number,
        "url": reference.url,
        "title": data.get("title"),
        "state": data.get("state"),
        "is_draft": data.get("isDraft", False),
        "author": author.get("login") if isinstance(author, dict) else None,
        "base_ref": data.get("baseRefName"),
        "head_ref": data.get("headRefName"),
        "head_sha": data.get("headRefOid"),
        "base_sha": data.get("baseRefOid"),
        "head_repository_url": _head_repository_url(
            reference, data.get("headRepository"), data.get("headRepositoryOwner")
        ),
        **_checks_fields(data),
        "body": data.get("body") or None,
        **_comments_payload(data),
        **_merge_fields(data),
    }
    if _PR_REVIEW_FIELDS.issubset(data):
        info["pr"]["reviewers"] = _shape_reviewers(data)
    if "labels" in data:
        info["pr"]["labels"] = _shape_labels(data["labels"])
    return info


def _pr_title(data: dict[str, Any] | None) -> str | None:
    title = data.get("title") if data else None
    if not isinstance(title, str):
        return None
    return title.strip() or None


def _gh_installed() -> bool:
    """Whether the ``gh`` CLI is on ``PATH``."""
    return shutil.which("gh") is not None


# A merged or closed PR's title is kept for the runner's life, and shared with the host's
# other runners this long; an open PR's title, or the answer that a PR can't be read, for
# 30 minutes. A failed lookup is retried after the failed-read window.
_FINISHED_TITLE_TTL_SECONDS = 30 * 24 * 3600.0
_OPEN_PR_TITLE_TTL_SECONDS = 30 * 60.0
_FINISHED_STATES = frozenset({"MERGED", "CLOSED"})
# One title query reads at most this many PRs.
_TITLE_QUERY_CHUNK = 50
# GraphQL error types that mean the PR or repository can't be read by this identity.
_UNREADABLE_ERROR_TYPES = frozenset({"NOT_FOUND", "FORBIDDEN"})
_FINISHED_TITLES_MAX = 4096
# Finished PRs' titles by title key, with when their lookup started; guarded by _preview_lock.
_finished_titles: dict[tuple[str, ...], tuple[float, str | None, str | None, bool]] = {}


def _title_from_shared_pr_data(identity: str, reference: PullRequestRef) -> dict[str, Any] | None:
    """The PR's entry in a fresh full, background or repository-listing read, if any."""
    repository, number = _repo_argument(reference).lower(), str(reference.number)
    for kind in ("full", "background"):
        hit, data = _cache_get(("pr", identity, repository, number, kind), shared=True)
        if hit and isinstance(data, dict):
            return data
    hit, listing = _cache_get(("pr-list", identity, repository), shared=True)
    for entry in listing if hit and isinstance(listing, list) else []:
        if isinstance(entry, dict) and entry.get("number") == reference.number:
            return entry
    return None


def _title_key(identity: str, reference: PullRequestRef) -> tuple[str, ...]:
    return ("pr", identity, _repo_argument(reference).lower(), str(reference.number), "title")


def _remember_finished_title(
    key: tuple[str, ...],
    title: str | None,
    started: float,
    state: str | None = None,
    is_draft: bool = False,
) -> None:
    """Keep a finished PR's title and state for the runner's life, barring a later write."""
    if _invalidated_since(key) >= started:
        return
    with _preview_lock:
        if key not in _finished_titles and len(_finished_titles) >= _FINISHED_TITLES_MAX:
            del _finished_titles[next(iter(_finished_titles))]
        _finished_titles[key] = (started, title, state, is_draft)


def _known_title(identity: str, reference: PullRequestRef) -> dict[str, Any] | None:
    """The title answer this runner or the host already holds, ``{title, timed_out}``, if any."""
    key = _title_key(identity, reference)
    with _preview_lock:
        finished = _finished_titles.get(key)
    if finished is not None:
        # Another runner's write to the PR stamps it; this runner's own write drops the entry.
        if _invalidated_since(key) < finished[0]:
            return {
                "title": finished[1],
                "timed_out": False,
                "state": finished[2],
                "is_draft": finished[3],
            }
        with _preview_lock:
            _finished_titles.pop(key, None)
    shared = _title_from_shared_pr_data(identity, reference)
    if shared is not None:
        return {
            "title": _pr_title(shared),
            "timed_out": False,
            "state": shared.get("state"),
            "is_draft": shared.get("isDraft") is True,
        }
    hit, answer = _cache_get(key, shared=True)
    if not hit or not isinstance(answer, dict):
        return None
    if answer.get("finished"):
        _remember_finished_title(
            key,
            answer.get("title"),
            float(answer.get("started") or 0),
            answer.get("state"),
            answer.get("is_draft") is True,
        )
    return answer


def _titles_query(references: Sequence[PullRequestRef]) -> list[str]:
    """``gh api graphql`` arguments reading each PR's title and state under its own alias.

    Owners and names travel as variables, so no repository name reaches the query text.
    """
    variables: list[str] = []
    fields: list[str] = []
    arguments: list[str] = []
    for index, reference in enumerate(references):
        owner, name = reference.repository.split("/", 1)
        variables.append(f"$o{index}: String!, $n{index}: String!")
        fields.append(
            f"p{index}: repository(owner: $o{index}, name: $n{index}) "
            f"{{ pullRequest(number: {int(reference.number)}) {{ title state isDraft }} }}"
        )
        arguments += ["-f", f"o{index}={owner}", "-f", f"n{index}={name}"]
    query = "query(" + ", ".join(variables) + ") {\n  " + "\n  ".join(fields) + "\n}"
    return ["-f", f"query={query}", *arguments]


def _graphql_titles(
    root: str, host: str, token: str | None, references: Sequence[PullRequestRef]
) -> dict[str, dict[str, Any] | None]:
    """Read the PRs' titles in one GraphQL query: ``{url: {title, state, isDraft} | None}``.

    ``None`` is a failed lookup. A PR or repository GitHub reports as not found or not
    readable answers ``{title: None, state: None}``; other PRs in the query still answer.
    """
    rc, out, err = _gh(
        ["api", "graphql", "--hostname", host, *_titles_query(references)], cwd=root, token=token
    )
    if rc != 0:
        _note_rate_limit(err, cwd=root, token=token)
    try:
        body = json.loads(out) if out.strip() else None
    except ValueError:
        body = None
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        return {reference.url: None for reference in references}
    errors: dict[str, set[str]] = {}
    reported = body.get("errors") if isinstance(body, dict) else None
    for error in reported if isinstance(reported, list) else []:
        path = error.get("path") if isinstance(error, dict) else None
        if isinstance(path, list) and path and isinstance(path[0], str):
            errors.setdefault(path[0], set()).add(str(error.get("type") or ""))
    found: dict[str, dict[str, Any] | None] = {}
    for index, reference in enumerate(references):
        alias = f"p{index}"
        node = data.get(alias)
        pull = node.get("pullRequest") if isinstance(node, dict) else None
        if isinstance(pull, dict):
            found[reference.url] = {
                "title": pull.get("title"),
                "state": pull.get("state"),
                "isDraft": pull.get("isDraft") is True,
            }
        elif alias in data and errors.get(alias, set()) <= _UNREADABLE_ERROR_TYPES:
            found[reference.url] = {"title": None, "state": None, "isDraft": False}
        else:
            found[reference.url] = None
    return found


def _query_titles(
    root: str,
    host: str,
    identity: str,
    token: str | None,
    references: Sequence[PullRequestRef],
    deadline: float,
) -> dict[str, dict[str, Any]]:
    """Look up titles no one holds, one query per chunk, shared by the host's runners.

    One lookup per host and identity runs at a time; a waiter then finds its titles cached.
    A chunk the deadline reaches before it starts is left out.
    """
    answers: dict[str, dict[str, Any]] = {}
    if not _gh_can_read_host(root, host, token):
        return {reference.url: _title_answer(None, False) for reference in references}
    for start in range(0, len(references), _TITLE_QUERY_CHUNK):
        if time.monotonic() >= deadline:
            break
        with _single_flight(("pr-titles", identity, host.lower())):
            missing: list[PullRequestRef] = []
            for reference in references[start : start + _TITLE_QUERY_CHUNK]:
                known = _known_title(identity, reference)
                if known is None:
                    missing.append(reference)
                else:
                    answers[reference.url] = known
            if not missing:
                continue
            if _background_reads_paused():  # asked again once the pause ends
                answers.update({reference.url: _title_answer(None, True) for reference in missing})
                continue
            started = time.time()
            found = _graphql_titles(root, host, token, missing)
            timed_out = _gh_timed_out.get()
            for reference in missing:
                pull = found.get(reference.url)
                finished = pull is not None and pull.get("state") in _FINISHED_STATES
                answer = {
                    "title": _pr_title(pull),
                    "timed_out": pull is None and timed_out,
                    "finished": finished,
                    "started": started,
                    "state": pull.get("state") if pull is not None else None,
                    "is_draft": pull is not None and pull.get("isDraft") is True,
                }
                if pull is None:
                    ttl = _FAILED_READ_TTL_SECONDS
                else:
                    ttl = _FINISHED_TITLE_TTL_SECONDS if finished else _OPEN_PR_TITLE_TTL_SECONDS
                key = _title_key(identity, reference)
                _cache_put(key, answer, ttl, shared=True, started=started)
                if finished:
                    _remember_finished_title(
                        key, answer["title"], started, answer["state"], answer["is_draft"]
                    )
                answers[reference.url] = answer
    return answers


def _title_answer(title: str | None, timed_out: bool) -> dict[str, Any]:
    """A title answer that carries no state, for a read that could not be made."""
    return {"title": title, "timed_out": timed_out, "state": None, "is_draft": False}


def _pr_titles_before(
    root: str, references: Sequence[PullRequestRef], deadline: float
) -> dict[str, tuple[str | None, bool]]:
    """Look up PRs' titles, with every ``gh`` call ending by ``deadline``.

    A fresh shared read of a PR answers it; otherwise one title entry per identity and
    PR is shared across sessions, a failed or timed-out lookup included, and the missing
    ones are read with one GraphQL query per host and identity. An entry is dropped with
    the PR's other entries when a write changes the PR.

    :param deadline: A ``time.monotonic()`` value.
    :returns: ``{url: (title, timed_out)}``; ``timed_out`` is true only when the deadline
        ended the lookup, or a rate-limit pause skipped a background one. A PR whose
        chunk the deadline reached first is left out.
    """
    return {
        url: (answer["title"], bool(answer["timed_out"]))
        for url, answer in _pr_answers_before(root, references, deadline).items()
    }


def _pr_statuses_before(
    root: str, references: Sequence[PullRequestRef], deadline: float
) -> dict[str, tuple[PullRequestStatus | None, bool]]:
    """Look up PRs' titles, states and draft flags with the batching and shared caches of
    :func:`_pr_titles_before`, so a refresh of many PRs is one query per host and identity.

    :returns: ``{url: (status, timed_out)}``; ``status`` is ``None`` when the PR could not be read.
        A PR whose chunk the deadline reached first is left out.
    """
    statuses: dict[str, tuple[PullRequestStatus | None, bool]] = {}
    for url, answer in _pr_answers_before(root, references, deadline).items():
        state = answer.get("state")
        status = (
            PullRequestStatus(
                title=answer["title"],
                state=state if state in ("OPEN", "MERGED", "CLOSED") else None,
                is_draft=answer.get("is_draft") is True,
            )
            if answer["title"] is not None or state is not None
            else None
        )
        statuses[url] = (status, bool(answer["timed_out"]))
    return statuses


def _pr_answers_before(
    root: str, references: Sequence[PullRequestRef], deadline: float
) -> dict[str, dict[str, Any]]:
    """The title answers behind :func:`_pr_titles_before` and :func:`_pr_statuses_before`."""
    answers: dict[str, dict[str, Any]] = {}
    groups: dict[tuple[str, str], tuple[str | None, list[PullRequestRef]]] = {}
    with _gh_deadline_scope(deadline):
        for reference in references:
            token, identity = _pr_read(root, reference)
            identity = identity or _unbound_identity(reference.host)
            known = _known_title(identity, reference)
            if known is not None:
                answers[reference.url] = known
            else:
                groups.setdefault((reference.host, identity), (token, []))[1].append(reference)
        for (host, identity), (token, missing) in groups.items():
            answers.update(_query_titles(root, host, identity, token, missing, deadline))
    return answers


def _pr_title_before(
    root: str, reference: PullRequestRef, deadline: float
) -> tuple[str | None, bool]:
    """Look up one PR's title like :func:`_pr_titles_before`."""
    return _pr_titles_before(root, [reference], deadline).get(reference.url, (None, True))


# What a title refresh reads: the state and draft flag come with the title in one call.
_PR_STATUS_FIELDS = "title,state,isDraft"


def _pr_status_before(
    root: str, reference: PullRequestRef, deadline: float
) -> tuple[PullRequestStatus | None, bool]:
    """Look up one PR's title, state and draft flag in the ``gh`` call a title lookup makes.

    :param deadline: A ``time.monotonic()`` value.
    :returns: ``(status, timed_out)``, as :func:`_pr_title_before` returns.
    """
    if _background_reads_paused():  # asked again once the pause ends
        return None, True
    with _gh_deadline_scope(deadline):
        data = _pr_json(root, reference, _PR_STATUS_FIELDS)
        if data is None:
            return None, _gh_timed_out.get()
    state = data.get("state")
    return (
        PullRequestStatus(
            title=_pr_title(data),
            state=state if state in ("OPEN", "MERGED", "CLOSED") else None,
            is_draft=data.get("isDraft") is True,
        ),
        False,
    )


def _verify_pr_access(root: str, reference: PullRequestRef) -> None:
    """Raise unless ``gh`` on the host can read the PR.

    :raises ValueError: When ``gh`` cannot read it.
    """
    if _pr_json(root, reference, "number,url") is None:
        raise ValueError("Cannot access this pull request using gh on the host")


def _adopt_workspace_account(root: str, reference: PullRequestRef) -> None:
    """Give a branch-inferred PR the workspace's account unless it has its own."""
    key = _workspace_key(root)
    account = _config.github_account_preference(key) if key else None
    if account and not _config.github_account_preference(_repo_argument(reference)):
        _config.set_github_account_preference(_repo_argument(reference), account)


def _set_preference(
    root: str,
    reference: PullRequestRef | None,
    *,
    account: str | None,
    remote: str | None,
) -> None:
    """Save a gh account choice for one PR or the workspace, and the base remote.

    The workspace account preference is keyed by the main worktree path, shared
    across a repo's worktrees, and persisted with
    :func:`omnigent.config.set_github_account_preference`; an empty ``account``
    clears it, falling back to ``gh``'s active account. The remote is
    ``gh repo set-default`` (persisted by ``gh`` in ``.git/config``); the base
    is normally resolved automatically, so this is only an escape hatch.

    :param reference: The PR whose account to set; ``None`` for the workspace.
    :param account: GitHub login to prefer, ``None`` to leave it, or empty to clear it.
    :param remote: Git remote name or ``owner/repo`` to set as the workspace's
        base repo; ignored with a ``reference``.
    """
    if reference is not None:
        if account is not None:
            _config.set_github_account_preference(_repo_argument(reference), account or None)
        return
    if remote:
        _gh(["repo", "set-default", remote], cwd=root)
    if account is not None:
        key = _workspace_key(root)
        if key:
            _config.set_github_account_preference(key, account or None)


def github_info(
    root: str, *, session_id: str | None = None, pr_url: str | None = None
) -> dict[str, Any]:
    """Return the panel payload; see :func:`pr_resource.pr_info`."""
    return pr_resource.pr_info(root, session_id=session_id, pr_url=pr_url)


def update_session_pr(root: str, session_id: str, url: str, action: str) -> dict[str, Any]:
    """Attach or remove a session PR; see :func:`pr_resource.update_session_pr`."""
    return pr_resource.update_session_pr(root, session_id, url, action)


def set_github_preference(
    root: str,
    *,
    account: str | None = None,
    remote: str | None = None,
    session_id: str | None = None,
    pr_url: str | None = None,
) -> dict[str, Any]:
    """Apply an account or remote choice; see :func:`pr_resource.set_pr_preference`."""
    return pr_resource.set_pr_preference(
        root, account=account, remote=remote, session_id=session_id, pr_url=pr_url
    )


def resolve_base_ref(root: str, base: str | None) -> str | None:
    """Return an explicit base branch, else the base of the workspace branch's PR.

    :param root: Absolute workspace path.
    :param base: Explicit base branch name, or ``None`` to derive the default.
    :returns: A base branch name, or ``None`` when none can be resolved.
    """
    if base:
        return base
    return _workspace_github_info(root).get("base_ref")


def _git_run(root: str) -> local_git.TextRun:
    """Run git commands in ``root`` through :func:`_git`, for :mod:`local_git`."""

    def run(argv: list[str]) -> tuple[int | None, str]:
        rc, out, _ = _git(argv, cwd=root)
        return rc, out

    return run


def _resolve_diff_base(root: str, base: str) -> str:
    """Resolve a base branch name to the ref to diff HEAD against.

    See :func:`omnigent.runner.git_providers.local_git.resolve_diff_base`.

    :raises OmnigentError: If the base is unavailable or shallow ancestry is missing.
    """
    return local_git.resolve_diff_base(_git_run(root), base)


def _read_diff_content(root: str, ref: str, path: str) -> str | None:
    """Read a local diff side; only a confirmed absent tree entry means no content."""
    return local_git.read_file(_git_run(root), ref, path)


# GitHub pulls/files ``status`` → the status vocabulary the web list uses.
_GH_STATUS_MAP = {
    "added": "created",
    "removed": "deleted",
    "modified": "modified",
    "renamed": "renamed",
    "copied": "created",
    "changed": "modified",
    "unchanged": "modified",
}


def _pr_number(root: str, *, token: str | None = None) -> int | None:
    """Return the PR number for the workspace's branch, or ``None``.

    :param root: Absolute workspace path.
    :param token: Optional GH_TOKEN to run ``gh`` as the selected account.
    :returns: The associated PR's number, or ``None`` when no PR resolves (none
        for the branch, ``gh`` missing, or not authenticated).
    """
    data = _pr_view_json(root, "number", token=token)
    if data is None:
        return None
    number = data.get("number")
    return number if isinstance(number, int) else None


def github_changed_files(
    root: str, *, session_id: str | None = None, pr_url: str | None = None
) -> dict[str, Any]:
    """List the selected PR's files; see :func:`pr_resource.pr_changed_files`."""
    return pr_resource.pr_changed_files(root, session_id=session_id, pr_url=pr_url)


def _pr_changed_files(root: str, reference: PullRequestRef | None) -> dict[str, Any]:
    """List the PR's changed files, straight from GitHub.

    Sourced from ``gh api .../pulls/<n>/files`` so the set (and each file's
    status / line counts) matches the PR's "Files changed" exactly — never a
    local ``git diff``. Empty when the branch has no PR.

    :param root: Absolute workspace path.
    :param reference: The tracked PR, or ``None`` for the workspace branch's PR.
    :returns: A ``list`` object whose ``data`` entries carry ``path`` / ``name``
        / ``status`` / ``lines_added`` / ``lines_removed``.
    """
    empty: dict[str, Any] = {"object": "list", "data": [], "has_more": False}
    token = _pr_token(root, reference) if reference else _account_token_for(root)
    number = reference.number if reference else _pr_number(root, token=token)
    if number is None:
        return empty
    repository = reference.repository if reference else "{owner}/{repo}"
    host_args = _host_args(root, reference) if reference else []
    rc, out, _ = _gh(
        [
            "api",
            *host_args,
            "--paginate",
            *(["--slurp"] if reference else []),
            f"repos/{repository}/pulls/{number}/files?per_page=100",
        ],
        cwd=root,
        token=token,
    )
    if rc != 0:
        raise ValueError("GitHub could not load the PR's changed files. Refresh to retry.")
    try:
        entries = json.loads(out)
    except ValueError:
        raise ValueError(
            "GitHub returned an invalid changed-file list. Refresh to retry."
        ) from None
    if not isinstance(entries, list):
        raise ValueError("GitHub returned an invalid changed-file list. Refresh to retry.")

    if reference:
        entries = [
            entry for page in entries for entry in (page if isinstance(page, list) else [page])
        ]
    data: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        # ``filename`` is the current path (the new name for a rename) — the one
        # the diff endpoint reads at HEAD, matching the whole-PR patch.
        path = entry.get("filename")
        if not path:
            continue
        data.append(
            {
                "object": "session.github.changed_file",
                "path": path,
                "name": str(path).split("/")[-1],
                "status": _GH_STATUS_MAP.get(str(entry.get("status")), "modified"),
                "lines_added": entry.get("additions"),
                "lines_removed": entry.get("deletions"),
                "previous_path": entry.get("previous_filename"),
            }
        )
    return {"object": "list", "data": data, "has_more": False}


def _pr_changed_files_at_revision(
    root: str, reference: PullRequestRef, *, head_sha: str, base_sha: str
) -> dict[str, Any]:
    """List changed files at the displayed revision; flag a list GitHub truncated."""
    with _gh_deadline_scope(time.monotonic() + _PR_EDIT_REQUEST_SECONDS):
        pr = _check_pr_revision(root, reference, head_sha=head_sha, base_sha=base_sha)
        result = _pr_changed_files(root, reference)
        _check_pr_revision(root, reference, head_sha=head_sha, base_sha=base_sha)
    total = pr.get("changed_files")
    total_count = total if isinstance(total, int) else None
    shown = len(result["data"])
    if total_count is not None and shown < total_count:
        result["has_more"] = True
        result["warning"] = (
            f"The provider lists at most {shown} files; {total_count - shown} files are not shown."
        )
    return {**result, "total_count": total_count, "head_sha": head_sha, "base_sha": base_sha}


def github_file_diff(
    root: str,
    base: str,
    path: str,
    *,
    session_id: str | None = None,
    pr_url: str | None = None,
    previous_path: str | None = None,
    head_sha: str | None = None,
    base_sha: str | None = None,
) -> dict[str, Any]:
    """Return one file's before/after; see :func:`pr_resource.pr_file_diff`."""
    return pr_resource.pr_file_diff(
        root,
        base,
        path,
        session_id=session_id,
        pr_url=pr_url,
        previous_path=previous_path,
        head_sha=head_sha,
        base_sha=base_sha,
    )


def _pr_file_diff(
    root: str,
    reference: PullRequestRef | None,
    path: str,
    *,
    base: str,
    previous_path: str | None,
    head_sha: str | None,
    base_sha: str | None,
) -> dict[str, Any]:
    """Return before/after content for one file, HEAD vs the base merge-base.

    :param root: Absolute workspace path.
    :param reference: The tracked PR, read from GitHub; ``None`` diffs the local
        checkout.
    :param path: Repo-root-relative path, as returned by
        :func:`github_changed_files`.
    :param base: Base branch name for the local diff, e.g. ``"main"``.
    :returns: A ``session.github.file_diff`` object with ``before`` (merge-base
        content, ``None`` for an added file) and ``after`` (HEAD content,
        ``None`` for a deleted file).
    :raises OmnigentError: If the local diff base or file content is unavailable.
    """
    if reference:
        return _pr_file_contents(
            root,
            reference,
            path,
            previous_path=previous_path,
            head_sha=head_sha,
            base_sha=base_sha,
        )
    resolved = resolve_base_ref(root, base or None)
    diff_base = _resolve_diff_base(root, resolved) if resolved else None

    before = _read_diff_content(root, diff_base, path) if diff_base is not None else None
    after = _read_diff_content(root, "HEAD", path)

    return {
        "object": "session.github.file_diff",
        "path": path,
        "before": before,
        "after": after,
    }


def github_pr_diff(
    root: str, *, session_id: str | None = None, pr_url: str | None = None
) -> dict[str, Any]:
    """Return the selected PR's patch; see :func:`pr_resource.pr_diff`."""
    return pr_resource.pr_diff(root, session_id=session_id, pr_url=pr_url)


def _pr_patch(root: str, reference: PullRequestRef | None) -> dict[str, Any]:
    """Return the whole PR as one unified diff patch, straight from GitHub.

    ``gh pr diff <number>`` yields the PR's "Files changed" patch (server-computed
    against the base's merge-base), which the web view parses client-side into
    per-file diffs. The PR is resolved by number first (via :func:`_pr_number`,
    which handles fork / triangular heads a bare ``gh pr diff`` can't); empty when
    the branch has no PR.

    :param root: Absolute workspace path.
    :param reference: The tracked PR, or ``None`` for the workspace branch's PR.
    :returns: A ``session.github.pr_diff`` object with the ``patch`` text
        (empty when there's no PR / no changes).
    """
    empty: dict[str, Any] = {"object": "session.github.pr_diff", "patch": ""}
    token = _pr_token(root, reference) if reference else _account_token_for(root)
    number = reference.number if reference else _pr_number(root, token=token)
    if number is None:
        return empty
    if reference:
        _host_args(root, reference)
    repo_args = ["-R", _repo_argument(reference)] if reference else []
    rc, out, _ = _gh(["pr", "diff", str(number), *repo_args], cwd=root, token=token)
    if rc != 0:
        raise ValueError("GitHub could not load the PR diff. Refresh or open the PR on GitHub.")
    return {"object": "session.github.pr_diff", "patch": out}


def _pr_patch_at_revision(
    root: str, reference: PullRequestRef, *, head_sha: str, base_sha: str
) -> dict[str, Any]:
    """Reject a patch if the PR moves before or during its fetch."""
    with _gh_deadline_scope(time.monotonic() + _PR_EDIT_REQUEST_SECONDS):
        _check_pr_revision(root, reference, head_sha=head_sha, base_sha=base_sha)
        result = _pr_patch(root, reference)
        _check_pr_revision(root, reference, head_sha=head_sha, base_sha=base_sha)
    return {**result, "head_sha": head_sha, "base_sha": base_sha}


def _check_pr_revision(
    root: str, reference: PullRequestRef, *, head_sha: str, base_sha: str | None = None
) -> dict[str, Any]:
    """Return the PR's REST payload, raising when its head, or its base if given, moved."""
    pr = _pr_api(root, reference, f"repos/{reference.repository}/pulls/{reference.number}")
    moved_head = _api_string(pr, "head", "sha") != head_sha
    moved_base = base_sha is not None and _api_string(pr, "base", "sha") != base_sha
    if moved_head or moved_base:
        raise ValueError("The pull request changed; refresh to see its latest revision.")
    return pr


# GitHub lists at most this many commits for one pull request.
_MAX_PR_COMMITS = 250


def _commit_payload(entry: dict[str, Any]) -> dict[str, Any]:
    """Shape one REST commit as ``session.github.commit``: title is the message's first line."""
    commit = entry.get("commit") if isinstance(entry.get("commit"), dict) else {}
    author = commit.get("author") if isinstance(commit.get("author"), dict) else {}
    message = commit.get("message") if isinstance(commit.get("message"), str) else ""
    return {
        "object": COMMIT_OBJECT,
        "sha": entry["sha"],
        "title": message.strip().split("\n", 1)[0].strip(),
        "author": author.get("name"),
        "date": author.get("date"),
    }


def _pr_commit_entries(root: str, reference: PullRequestRef) -> list[dict[str, Any]]:
    """Fetch every commit page GitHub returns for the PR, oldest first."""
    rc, out, _ = _gh(
        [
            "api",
            *_host_args(root, reference),
            "--paginate",
            "--slurp",
            f"repos/{reference.repository}/pulls/{reference.number}/commits?per_page=100",
        ],
        cwd=root,
        token=_pr_token(root, reference),
    )
    if rc != 0:
        raise ValueError("GitHub could not load the PR's commits. Refresh to retry.")
    try:
        pages = json.loads(out)
    except ValueError:
        raise ValueError("GitHub returned an invalid commit list. Refresh to retry.") from None
    if not isinstance(pages, list):
        raise ValueError("GitHub returned an invalid commit list. Refresh to retry.")
    entries = [entry for page in pages for entry in (page if isinstance(page, list) else [page])]
    return [
        _commit_payload(entry)
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("sha"), str)
    ]


def _pr_commits(root: str, reference: PullRequestRef, *, head_sha: str) -> dict[str, Any]:
    """List the PR's commits at ``head_sha``, capped at 250; ``has_more`` reports the rest.

    The PR's head is checked before and after the listing, so the list describes the
    revision the panel shows.
    """
    with _gh_deadline_scope(time.monotonic() + _PR_EDIT_REQUEST_SECONDS):
        pr = _check_pr_revision(root, reference, head_sha=head_sha)
        commits = _pr_commit_entries(root, reference)
        _check_pr_revision(root, reference, head_sha=head_sha)
        shown = commits[:_MAX_PR_COMMITS]
        heads = _other_pr_heads(root, reference, pr, [commit["sha"] for commit in shown])
    for commit in shown:
        if commit["sha"] in heads:
            commit["head_of"] = heads[commit["sha"]]
    total = pr.get("commits")
    return {
        "object": "list",
        "data": shown,
        "has_more": isinstance(total, int) and total > len(shown),
    }


# How many of the author's recently updated PRs one lookup reads for stack parents.
_AUTHOR_PR_LOOKUPS = 100
# The lookup only adds hints, so a slow search must not cost the commit list its deadline.
_AUTHOR_PR_LOOKUP_SECONDS = 4.0
_AUTHOR_PRS_QUERY = """query($search: String!, $first: Int!) {
  search(query: $search, type: ISSUE, first: $first) {
    nodes { ... on PullRequest { number url state headRefOid } }
  }
}"""


def _other_pr_heads(
    root: str, reference: PullRequestRef, pr: dict[str, Any], shas: list[str]
) -> dict[str, list[dict[str, Any]]]:
    """Map each commit that is the head of another PR by the same author, best effort.

    Marks a stacked PR's parents in its commit list with one search over the author's
    recently updated PRs in the repository; a per-commit lookup is too slow on large
    repositories. Any failure leaves the commits unmarked.
    """
    user = pr.get("user") if isinstance(pr.get("user"), dict) else {}
    author = user.get("login")
    if not isinstance(author, str) or not author or not shas:
        return {}
    search = f"repo:{reference.repository} is:pr author:{author} sort:updated-desc"
    try:
        rc, out, _ = _gh(
            [
                "api",
                *_host_args(root, reference),
                "graphql",
                "-f",
                f"query={_AUTHOR_PRS_QUERY}",
                "-f",
                f"search={search}",
                "-F",
                f"first={_AUTHOR_PR_LOOKUPS}",
            ],
            cwd=root,
            token=_pr_token(root, reference),
            timeout=_AUTHOR_PR_LOOKUP_SECONDS,
        )
    except ValueError:
        return {}
    if rc != 0:
        return {}
    try:
        nodes = json.loads(out)["data"]["search"]["nodes"]
    except (ValueError, KeyError, TypeError):
        return {}
    wanted = set(shas)
    heads: dict[str, list[dict[str, Any]]] = {}
    for node in nodes if isinstance(nodes, list) else []:
        if not isinstance(node, dict) or node.get("number") == reference.number:
            continue
        sha = node.get("headRefOid")
        if isinstance(sha, str) and sha in wanted:
            heads.setdefault(sha, []).append(
                {"number": node.get("number"), "url": node.get("url"), "state": node.get("state")}
            )
    return heads


def _pr_commit_patch(root: str, reference: PullRequestRef, *, commit_sha: str) -> dict[str, Any]:
    """Return one commit's patch, read as GitHub's diff media type for that commit.

    The commit must be one of the pull request's own commits; otherwise any commit
    the token can read in the repository would be served. ``commit_sha`` is a full
    object id, so the patch is the commit the user selected.
    """
    with _gh_deadline_scope(time.monotonic() + _PR_EDIT_REQUEST_SECONDS):
        if commit_sha not in {entry["sha"] for entry in _pr_commit_entries(root, reference)}:
            raise ValueError("That commit is not part of the selected pull request.")
        rc, out, _ = _gh(
            [
                "api",
                *_host_args(root, reference),
                "-H",
                "Accept: application/vnd.github.diff",
                f"repos/{reference.repository}/commits/{commit_sha}",
            ],
            cwd=root,
            token=_pr_token(root, reference),
        )
    if rc != 0:
        raise ValueError("GitHub could not load the commit's diff. Refresh or open it on GitHub.")
    return {"object": PR_DIFF_OBJECT, "patch": out}


_COMPARE_FAILED = "GitHub could not load the changes since that commit. Refresh to retry."


def _pr_compare(
    root: str, reference: PullRequestRef, *, head_sha: str, from_sha: str
) -> dict[str, Any]:
    """Return the PR's changes after ``from_sha``, up to its head.

    ``from_sha`` must be one of the PR's own commits, so only the PR's history is
    served, and the head is checked before and after the reads so the patch describes
    the revision the panel shows.
    """
    with _gh_deadline_scope(time.monotonic() + _PR_EDIT_REQUEST_SECONDS):
        host = _host_args(root, reference)
        token = _pr_token(root, reference)
        _check_pr_revision(root, reference, head_sha=head_sha)
        if from_sha not in {entry["sha"] for entry in _pr_commit_entries(root, reference)}:
            raise ValueError("That commit is not part of the selected pull request.")
        rc, out, _ = _gh(
            [
                "api",
                *host,
                "-H",
                "Accept: application/vnd.github.diff",
                f"repos/{reference.repository}/compare/{from_sha}...{head_sha}",
            ],
            cwd=root,
            token=token,
        )
        if rc != 0:
            raise ValueError(_COMPARE_FAILED)
        _check_pr_revision(root, reference, head_sha=head_sha)
    return {"object": PR_DIFF_OBJECT, "patch": out}


def _host_args(root: str, reference: PullRequestRef) -> list[str]:
    if reference.host != "github.com" and not _gh_can_read_host(
        root, reference.host, _pr_token(root, reference)
    ):
        raise ValueError("Sign in to this GitHub host with gh before viewing its PRs")
    return ["--hostname", reference.host]


def _pr_api(root: str, reference: PullRequestRef, endpoint: str) -> dict[str, Any]:
    rc, out, _ = _gh(
        ["api", *_host_args(root, reference), endpoint],
        cwd=root,
        token=_pr_token(root, reference),
    )
    if rc != 0:
        raise ValueError("GitHub could not load the selected PR's file content")
    try:
        result = json.loads(out)
    except ValueError as exc:
        raise ValueError("GitHub returned an unexpected file response") from exc
    if not isinstance(result, dict):
        raise ValueError("GitHub returned an unexpected file response")
    return result


def _api_string(value: object, *keys: str) -> str:
    """Read a required nonempty string from a GitHub API response."""
    for key in keys:
        if not isinstance(value, dict):
            raise ValueError("GitHub returned an unexpected file response")
        value = value.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError("GitHub returned an unexpected file response")
    return value


def _pr_file_contents(
    root: str,
    reference: PullRequestRef,
    path: str,
    *,
    previous_path: str | None,
    head_sha: str | None,
    base_sha: str | None,
) -> dict[str, Any]:
    for candidate in (path, previous_path or path):
        if candidate.startswith("/") or any(p in {"", ".."} for p in candidate.split("/")):
            raise ValueError("Invalid repository-relative path")
    pr = _pr_api(root, reference, f"repos/{reference.repository}/pulls/{reference.number}")
    current_head = _api_string(pr, "head", "sha")
    current_base = _api_string(pr, "base", "sha")
    if (head_sha and head_sha != current_head) or (base_sha and base_sha != current_base):
        raise ValueError("The pull request changed; refresh before expanding context")
    head_repo = pr["head"].get("repo")
    if not isinstance(head_repo, dict):
        raise ValueError("The pull request's head repository is no longer available")
    head_repository = _api_string(head_repo, "full_name")
    comparison = _pr_api(
        root,
        reference,
        f"repos/{reference.repository}/compare/{current_base}...{current_head}",
    )
    merge_base = _api_string(comparison, "merge_base_commit", "sha")

    def contents(repository: str, ref: str, filename: str) -> str | None:
        # A missing side is expected for additions/deletions. Other failures stay visible.
        endpoint = f"repos/{repository}/contents/{quote(filename, safe='/')}?ref={quote(ref)}"
        rc, out, err = _gh(
            ["api", *_host_args(root, reference), endpoint],
            cwd=root,
            token=_pr_token(root, reference),
        )
        if rc != 0:
            if "HTTP 404" in err:
                return None
            raise ValueError("GitHub could not load the selected file revision")
        try:
            value = json.loads(out)
            if (
                not isinstance(value, dict)
                or value.get("encoding") != "base64"
                or not isinstance(value.get("content"), str)
            ):
                raise ValueError("Unexpected file content")
            text = base64.b64decode(value["content"]).decode("utf-8")
            if "\x00" in text:
                raise ValueError("Binary content")
            return text
        except (ValueError, UnicodeError) as exc:
            raise ValueError("Expanded context is unavailable for this file") from exc

    return {
        "object": "session.github.file_diff",
        "path": path,
        "before": contents(reference.repository, merge_base, previous_path or path),
        "after": contents(head_repository, current_head, path),
    }


def _repo_labels(root: str, reference: PullRequestRef) -> dict[str, Any]:
    """List the PR repository's labels by name for the label picker.

    :raises ValueError: When ``gh`` cannot list them within :data:`_PR_EDIT_REQUEST_SECONDS`.
    """
    with _gh_deadline_scope(time.monotonic() + _PR_EDIT_REQUEST_SECONDS):
        _host_args(root, reference)
        rc, out, _ = _gh(
            [
                "label",
                "list",
                "-R",
                _repo_argument(reference),
                "--json",
                "name,color,description",
                "--limit",
                # One past the cap tells a full list from a truncated one.
                str(_MAX_REPO_LABELS + 1),
            ],
            cwd=root,
            token=_pr_token(root, reference),
        )
    if rc != 0:
        raise ValueError("GitHub could not list this repository's labels. Refresh to retry.")
    try:
        raw = json.loads(out)
    except ValueError as exc:
        raise ValueError("GitHub returned an unexpected label list") from exc
    raw = raw if isinstance(raw, list) else []
    labels = sorted(
        _shape_labels(raw[:_MAX_REPO_LABELS]), key=lambda label: str(label["name"]).casefold()
    )
    return {"object": "list", "data": labels, "has_more": len(raw) > _MAX_REPO_LABELS}


def _labels_api(
    root: str, host: list[str], token: str | None, args: list[str], *, missing_ok: bool = False
) -> list[Any] | None:
    """Run one ``gh api`` issue-labels request and return the PR's labels after it.

    :param missing_ok: Return ``None`` when GitHub reports that the PR lacks the
        label being removed.
    :raises ValueError: With a user-facing message when GitHub refuses the request.
    """
    rc, out, err = _gh(["api", *host, *args], cwd=root, token=token)
    try:
        body = json.loads(out) if out.strip() else None
    except ValueError:
        body = None
    if rc == 0 and isinstance(body, list):
        return body
    message = body.get("message") if isinstance(body, dict) else None
    if missing_ok and message == "Label does not exist":
        return None
    raise _pr_edit_error(err, message, "label")


def _pr_edit_error(err: str, message: object, subject: str) -> ValueError:
    """The user-facing error for a refused ``gh api`` edit of the PR's ``subject``s."""
    if re.search(r"\(HTTP 40[134]\)", err):
        return ValueError(
            f"The GitHub account on the host cannot edit this pull request's {subject}s."
        )
    if isinstance(message, str) and "(HTTP 422)" in err:
        return ValueError(f"GitHub rejected the {subject} change: {message}")
    return ValueError(f"GitHub could not update the {subject}s. Refresh to retry.")


def _edit_labels(
    root: str, reference: PullRequestRef, *, add: Sequence[str], remove: Sequence[str]
) -> dict[str, Any]:
    """Remove, then add, the PR's labels through GitHub's issue labels API.

    Every ``gh`` call, including the account and host lookups, shares
    :data:`_PR_EDIT_REQUEST_SECONDS`; running out reports that the outcome is unknown.
    """
    with _gh_deadline_scope(time.monotonic() + _PR_EDIT_REQUEST_SECONDS):
        try:
            return _apply_label_edit(root, reference, add=add, remove=remove)
        except ValueError:
            if _gh_timed_out.get():
                raise ValueError(_LABEL_EDIT_TIMED_OUT) from None
            raise


def _apply_label_edit(
    root: str, reference: PullRequestRef, *, add: Sequence[str], remove: Sequence[str]
) -> dict[str, Any]:
    """Make the edit's requests; each returns the PR's labels, so the last one is the result.

    A final read supplies the labels when every removed label was already absent.
    """
    host = _host_args(root, reference)
    token = _pr_token(root, reference)
    if _gh_timed_out.get():
        # The selected account's token is unknown, so the edit must not run as another.
        raise ValueError(_LABEL_EDIT_TIMED_OUT)
    endpoint = f"repos/{reference.repository}/issues/{reference.number}/labels"
    labels: list[Any] | None = None
    for name in remove:
        removed = _labels_api(
            root,
            host,
            token,
            ["-X", "DELETE", f"{endpoint}/{quote(name, safe='')}"],
            missing_ok=True,
        )
        labels = removed if removed is not None else labels
    if add:
        fields = [arg for name in add for arg in ("-f", f"labels[]={name}")]
        labels = _labels_api(root, host, token, ["-X", "POST", endpoint, *fields])
    if labels is None:
        labels = _labels_api(root, host, token, [f"{endpoint}?per_page=100"])
    return {"object": PR_LABELS_OBJECT, "labels": _shape_labels(labels)}


def _merge_options(root: str, reference: PullRequestRef) -> dict[str, Any]:
    """Return the merge methods the PR's repository allows and the one to preselect.

    The default is the viewer's last-used method, or the repository default,
    when it is allowed; otherwise the first allowed method.

    :raises ValueError: When ``gh`` cannot read the repository's merge settings.
    """
    fields = ",".join(["viewerDefaultMergeMethod", *(field for _, field in _MERGE_METHODS)])
    with _gh_deadline_scope(time.monotonic() + _PR_EDIT_REQUEST_SECONDS):
        _host_args(root, reference)
        rc, out, _ = _gh(
            ["repo", "view", _repo_argument(reference), "--json", fields],
            cwd=root,
            token=_pr_token(root, reference),
        )
    if rc != 0:
        raise ValueError(
            "GitHub could not read this repository's merge settings. Refresh to retry."
        )
    try:
        data = json.loads(out)
    except ValueError as exc:
        raise ValueError("GitHub returned unexpected merge settings") from exc
    if not isinstance(data, dict):
        raise ValueError("GitHub returned unexpected merge settings")
    methods = [method for method, field in _MERGE_METHODS if data.get(field) is True]
    preferred = data.get("viewerDefaultMergeMethod")
    preferred = preferred.lower() if isinstance(preferred, str) else None
    default = preferred if preferred in methods else next(iter(methods), None)
    return {"object": MERGE_OPTIONS_OBJECT, "methods": methods, "default": default}


def _merge_error(stderr: str, *, action: str) -> str:
    """Turn ``gh pr merge``'s error output into a user-facing message.

    :param action: What failed, e.g. ``"merge this pull request"``.
    """
    if "Head branch was modified" in stderr:
        return (
            "The pull request's branch changed since the panel loaded. "
            "Review the new commits, then merge again."
        )
    # An Enterprise Managed User can read public repositories outside its
    # enterprise but not write to them; GitHub says so in a GraphQL error, not a 403.
    if re.search(
        r"\(HTTP 40[13]\)|Resource not accessible|must have (?:write|push)"
        r"|As an Enterprise Managed User",
        stderr,
    ):
        return f"The GitHub account on the host cannot {action}."
    detail = next((line.strip() for line in stderr.splitlines() if line.strip()), "")
    # gh prefixes GraphQL errors and its failure marks; neither helps the reader.
    detail = re.sub(r"^(?:X |GraphQL: )+", "", detail).strip()[:300]
    if not detail:
        return f"GitHub could not {action}. Refresh to retry."
    return f"GitHub could not {action}: {detail}"


def _active_login(root: str, host: str) -> str | None:
    """``gh``'s active account on ``host``, or ``None`` when there is none."""
    _, accounts = _cached_accounts(root)
    return next(
        (a.get("login") for a in accounts if a.get("host") == host and a.get("active")), None
    )


def _run_pr_merge(
    root: str,
    reference: PullRequestRef,
    flags: list[str],
    *,
    action: str,
    timed_out: str,
    account: str | None = None,
) -> None:
    """Run ``gh pr merge <flags>`` on the PR as ``account`` or its selected account.

    Every ``gh`` call shares :data:`_PR_EDIT_REQUEST_SECONDS`. ``account`` binds
    this call alone; it is never read back from the saved preference, which a
    concurrent request can change. Without either, ``gh``'s active account runs
    it; a named account without a token refuses. A sandbox keeps its single
    broker identity, so it refuses any other ``account``.

    :param action: What the command does, for error messages.
    :param timed_out: Message when the deadline leaves the outcome unknown.
    :param account: The account the user confirmed for this command.
    :raises ValueError: With a user-facing message when GitHub refuses.
    """
    with _gh_deadline_scope(time.monotonic() + _PR_EDIT_REQUEST_SECONDS):
        _host_args(root, reference)
        sandbox_login: str | None = None
        if account is None:
            token = _pr_token(root, reference)
            login = (
                None
                if _in_sandbox()
                else _config.github_account_preference(_repo_argument(reference))
            )
        elif _in_sandbox():
            token, login = None, None
            sandbox_login = _active_login(root, reference.host)
        else:
            token, login = _gh_auth_token(root, account, reference.host), account
        # Without the selected account's token gh would run as another account.
        if _gh_timed_out.get():
            raise ValueError(timed_out)
        if account is not None and _in_sandbox() and sandbox_login != account:
            raise ValueError(
                "This sandbox can only use its own GitHub account"
                + (f", {sandbox_login}." if sandbox_login else ".")
            )
        if login and token is None:
            raise ValueError(
                f"The selected GitHub account {login} isn't signed in on the host. "
                "Sign in with gh or choose another account."
            )
        rc, _, err = _gh(
            ["pr", "merge", str(reference.number), "-R", _repo_argument(reference), *flags],
            cwd=root,
            token=token,
        )
        if rc != 0:
            raise ValueError(
                timed_out if _gh_timed_out.get() else _merge_error(err, action=action)
            )


def _merge_pr(
    root: str,
    reference: PullRequestRef,
    *,
    merge_method: str,
    expected_head: str,
    account: str | None = None,
) -> dict[str, Any]:
    """Merge the PR with ``gh pr merge``, only while its head is ``expected_head``.

    ``--match-head-commit`` makes GitHub refuse the merge when the branch moved
    since the panel showed it.

    :param account: Run as this account instead of the PR's saved one.
    :raises ValueError: With a user-facing message when GitHub refuses the merge.
    """
    _run_pr_merge(
        root,
        reference,
        [f"--{merge_method}", "--match-head-commit", expected_head],
        action="merge this pull request",
        timed_out=_MERGE_TIMED_OUT,
        account=account,
    )
    return {"object": PR_MERGE_OBJECT, "pr_url": reference.url}


def _disable_auto_merge(root: str, reference: PullRequestRef) -> dict[str, Any]:
    """Turn off the PR's auto-merge with ``gh pr merge --disable-auto``.

    :raises ValueError: With a user-facing message when GitHub refuses.
    """
    _run_pr_merge(
        root,
        reference,
        ["--disable-auto"],
        action="disable auto-merge",
        timed_out=_DISABLE_AUTO_MERGE_TIMED_OUT,
    )
    return {"object": PR_MERGE_OBJECT, "pr_url": reference.url}


# The reviewer picker lists at most this many of the repository's assignable users.
_MAX_REVIEW_CANDIDATES = 1000
# One page of the repository's teams; a repository rarely grants more.
_MAX_REVIEW_TEAMS = 100
_REVIEWER_EDIT_TIMED_OUT = (
    "GitHub took too long to update the reviewers. Refresh to see which applied."
)
_ASSIGNABLE_USERS_QUERY = """
query($owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    assignableUsers(first: 100, after: $after) {
      nodes { login name }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""


def _assignable_users(
    root: str, host: list[str], token: str | None, reference: PullRequestRef
) -> tuple[list[dict[str, Any]], bool]:
    """Page through the repository's assignable users, who can be asked to review.

    :returns: ``(users, has_more)``, capped at :data:`_MAX_REVIEW_CANDIDATES`.
    :raises ValueError: When ``gh`` cannot list them.
    """
    owner, name = reference.repository.split("/", 1)
    users: list[dict[str, Any]] = []
    after: str | None = None
    while True:
        args = ["api", "graphql", *host, "-f", f"query={_ASSIGNABLE_USERS_QUERY}"]
        args += ["-f", f"owner={owner}", "-f", f"name={name}"]
        if after:
            args += ["-f", f"after={after}"]
        rc, out, _ = _gh(args, cwd=root, token=token)
        try:
            page = json.loads(out)["data"]["repository"]["assignableUsers"] if rc == 0 else None
        except (ValueError, KeyError, TypeError):
            page = None
        if not isinstance(page, dict) or not isinstance(page.get("nodes"), list):
            raise ValueError(
                "GitHub could not list this repository's reviewers. Refresh to retry."
            )
        for node in page["nodes"]:
            login = node.get("login") if isinstance(node, dict) else None
            if isinstance(login, str) and login:
                display = node.get("name")
                users.append(
                    {
                        "login": login,
                        "kind": "user",
                        "name": display if isinstance(display, str) and display else None,
                    }
                )
        info = page.get("pageInfo") if isinstance(page.get("pageInfo"), dict) else {}
        cursor = info.get("endCursor")
        if not info.get("hasNextPage") or not isinstance(cursor, str):
            return users, False
        if len(users) >= _MAX_REVIEW_CANDIDATES:
            return users[:_MAX_REVIEW_CANDIDATES], True
        after = cursor


def _repo_teams(
    root: str, host: list[str], token: str | None, reference: PullRequestRef
) -> tuple[list[dict[str, Any]], bool]:
    """List the teams with access to the repository; none when ``gh`` cannot.

    A user-owned repository has no teams, and listing them may need more access
    than requesting a review does, so a failure only leaves the teams out.
    """
    rc, out, _ = _gh(
        ["api", *host, f"repos/{reference.repository}/teams?per_page={_MAX_REVIEW_TEAMS}"],
        cwd=root,
        token=token,
    )
    try:
        raw = json.loads(out) if rc == 0 else []
    except ValueError:
        raw = []
    teams: list[dict[str, Any]] = []
    for team in raw if isinstance(raw, list) else []:
        slug = team.get("slug") if isinstance(team, dict) else None
        if isinstance(slug, str) and slug:
            display = team.get("name")
            teams.append(
                {
                    "login": slug,
                    "kind": "team",
                    "name": display if isinstance(display, str) and display else None,
                }
            )
    return teams, isinstance(raw, list) and len(raw) >= _MAX_REVIEW_TEAMS


def _review_candidates(root: str, reference: PullRequestRef) -> dict[str, Any]:
    """List the users and teams the reviewer picker offers, each sorted by login.

    :raises ValueError: When ``gh`` cannot list the users within
        :data:`_PR_EDIT_REQUEST_SECONDS`.
    """
    with _gh_deadline_scope(time.monotonic() + _PR_EDIT_REQUEST_SECONDS):
        host = _host_args(root, reference)
        token = _pr_token(root, reference)
        users, more_users = _assignable_users(root, host, token, reference)
        teams, more_teams = _repo_teams(root, host, token, reference)

    def by_login(candidate: dict[str, Any]) -> str:
        return str(candidate["login"]).casefold()

    return {
        "object": "list",
        "data": sorted(users, key=by_login) + sorted(teams, key=by_login),
        "has_more": more_users or more_teams,
    }


def _reviewer_fields(reviewers: Sequence[ReviewerRef]) -> list[str]:
    """``gh api`` fields naming ``reviewers`` in a requested reviewers request body.

    A withdrawal must carry ``reviewers`` even for teams alone, so ``reviewers[]``
    without a value sends an empty array then.
    """
    fields = [
        arg
        for reviewer in reviewers
        for arg in (
            "-f",
            f"{'team_reviewers' if reviewer.kind == 'team' else 'reviewers'}[]={reviewer.login}",
        )
    ]
    if all(reviewer.kind == "team" for reviewer in reviewers):
        fields = ["-f", "reviewers[]", *fields]
    return fields


def _reviewers_api(root: str, host: list[str], token: str | None, args: list[str]) -> None:
    """Run one ``gh api`` requested reviewers request.

    :raises ValueError: With a user-facing message when GitHub refuses the request.
    """
    rc, out, err = _gh(["api", *host, *args], cwd=root, token=token)
    if rc == 0:
        return
    try:
        body = json.loads(out) if out.strip() else None
    except ValueError:
        body = None
    raise _pr_edit_error(err, body.get("message") if isinstance(body, dict) else None, "reviewer")


def _edit_reviewers(
    root: str,
    reference: PullRequestRef,
    *,
    add: Sequence[ReviewerRef],
    remove: Sequence[ReviewerRef],
) -> dict[str, Any]:
    """Withdraw, then request, reviews through GitHub's requested reviewers API.

    Every ``gh`` call shares :data:`_PR_EDIT_REQUEST_SECONDS`; running out reports
    that the outcome is unknown.
    """
    with _gh_deadline_scope(time.monotonic() + _PR_EDIT_REQUEST_SECONDS):
        try:
            return _apply_reviewer_edit(root, reference, add=add, remove=remove)
        except ValueError:
            if _gh_timed_out.get():
                raise ValueError(_REVIEWER_EDIT_TIMED_OUT) from None
            raise


def _apply_reviewer_edit(
    root: str,
    reference: PullRequestRef,
    *,
    add: Sequence[ReviewerRef],
    remove: Sequence[ReviewerRef],
) -> dict[str, Any]:
    """Make the edit's requests, then read the PR's reviewers with their review states."""
    host = _host_args(root, reference)
    token = _pr_token(root, reference)
    if _gh_timed_out.get():
        # The selected account's token is unknown, so the edit must not run as another.
        raise ValueError(_REVIEWER_EDIT_TIMED_OUT)
    endpoint = f"repos/{reference.repository}/pulls/{reference.number}/requested_reviewers"
    if remove:
        _reviewers_api(root, host, token, ["-X", "DELETE", endpoint, *_reviewer_fields(remove)])
    if add:
        _reviewers_api(root, host, token, ["-X", "POST", endpoint, *_reviewer_fields(add)])
    data = _gh_pr_view(
        [str(reference.number), "-R", _repo_argument(reference)],
        ",".join(sorted(_PR_REVIEW_FIELDS)),
        cwd=root,
        token=token,
    )
    if data is None or not _PR_REVIEW_FIELDS.issubset(data):
        raise ValueError(
            "GitHub updated the reviewers but could not list them. Refresh to see them."
        )
    return {"object": PR_REVIEWERS_OBJECT, "reviewers": _shape_reviewers(data)}


_DETAILS_EDIT_TIMED_OUT = (
    "GitHub took too long to update the pull request. Refresh to see whether it applied."
)
# One attachment upload's ``gh`` calls share this budget, under the server's wait,
# so a video has time to reach GitHub.
_ATTACHMENT_REQUEST_SECONDS = 110.0
_ATTACHMENT_TIMED_OUT = "GitHub took too long to upload the file. Try again."


def _pr_json_write(
    root: str,
    *,
    host: list[str],
    token: str | None,
    method: str,
    endpoint: str,
    fields: dict[str, Any],
) -> tuple[int | None, str, str]:
    """Send Markdown in a JSON file so shell syntax and long bodies remain literal."""
    with tempfile.NamedTemporaryFile(
        "w", suffix=".json", encoding="utf-8", delete_on_close=False
    ) as payload:
        json.dump(fields, payload)
        payload.close()
        return _gh(
            ["api", *host, "-X", method, endpoint, "--input", payload.name],
            cwd=root,
            token=token,
        )


def _create_comment(root: str, reference: PullRequestRef, *, body: str) -> dict[str, Any]:
    """Post once and return GitHub's canonical comment, without a follow-up read."""
    with _gh_deadline_scope(time.monotonic() + _PR_EDIT_REQUEST_SECONDS):
        result = _post_comment(
            root,
            reference,
            endpoint=f"repos/{reference.repository}/issues/{reference.number}/comments",
            body=body,
        )
    author = result.get("user")
    return {
        "object": "session.github.pr_comment",
        "comment": {
            "author": author.get("login") if isinstance(author, dict) else None,
            "body": result.get("body") or "",
            "created_at": result.get("created_at"),
            "url": result["html_url"],
        },
    }


def _post_comment(
    root: str, reference: PullRequestRef, *, endpoint: str, body: str
) -> dict[str, Any]:
    """Publish once within the caller's deadline; preserve uncertainty after dispatch."""
    uncertain = "Could not confirm comment posting. Check GitHub before retrying."
    host = _host_args(root, reference)
    token = _pr_token(root, reference)
    if _gh_timed_out.get():
        raise ValueError("GitHub authentication timed out. Your comment was not sent.")
    rc, out, err = _pr_json_write(
        root,
        host=host,
        token=token,
        method="POST",
        endpoint=endpoint,
        fields={"body": body},
    )
    if _gh_timed_out.get():
        raise ValueError(uncertain)
    try:
        result = json.loads(out)
    except ValueError:
        result = None
    if rc != 0 or not isinstance(result, dict):
        if re.search(r"\(HTTP 40[134]\)", err):
            raise ValueError("The GitHub account on the host cannot post to this conversation.")
        if "(HTTP 422)" in err:
            raise ValueError("GitHub rejected the comment. Check its length and try again later.")
        raise ValueError(uncertain)
    if not result.get("id") or not isinstance(result.get("html_url"), str):
        raise ValueError(uncertain)
    return result


def _edit_details(
    root: str, reference: PullRequestRef, *, title: str | None, body: str | None
) -> dict[str, Any]:
    """Update the PR's title and/or body with one pull requests API ``PATCH``.

    The body travels in a JSON file, since a long description could pass the
    command-line length limit as a field.
    """
    with _gh_deadline_scope(time.monotonic() + _PR_EDIT_REQUEST_SECONDS):
        host = _host_args(root, reference)
        token = _pr_token(root, reference)
        if _gh_timed_out.get():
            # The selected account's token is unknown, so the edit must not run as another.
            raise ValueError(_DETAILS_EDIT_TIMED_OUT)
        fields = {
            key: value for key, value in (("title", title), ("body", body)) if value is not None
        }
        rc, out, err = _pr_json_write(
            root,
            host=host,
            token=token,
            method="PATCH",
            endpoint=f"repos/{reference.repository}/pulls/{reference.number}",
            fields=fields,
        )
        if _gh_timed_out.get():
            raise ValueError(_DETAILS_EDIT_TIMED_OUT)
    try:
        result = json.loads(out) if out.strip() else None
    except ValueError:
        result = None
    if rc != 0 or not isinstance(result, dict):
        message = result.get("message") if isinstance(result, dict) else None
        if re.search(r"\(HTTP 40[134]\)", err):
            raise ValueError("The GitHub account on the host cannot edit this pull request.")
        if isinstance(message, str) and "(HTTP 422)" in err:
            raise ValueError(f"GitHub rejected the change: {message}")
        raise ValueError("GitHub could not update the pull request. Refresh to retry.")
    body_after = result.get("body")
    return {
        "object": PR_DETAILS_OBJECT,
        "title": str(result.get("title") or ""),
        "body": body_after if isinstance(body_after, str) else "",
    }


def _attachments_endpoint(host: str) -> str:
    """The user attachments upload URL that ``gh --attach`` posts to on ``host``."""
    if host == "github.com":
        return "https://uploads.github.com/user-attachments/assets"
    return f"https://{host}/api/uploads/user-attachments/assets"


def _is_attachment_url(url: str, host: str) -> bool:
    """Whether ``url`` is a user-attachments asset on ``host``, as an upload returns."""
    return (
        re.fullmatch(rf"https://{re.escape(host)}/user-attachments/assets/[0-9a-f-]+", url)
        is not None
    )


def _upload_attachment(
    root: str, reference: PullRequestRef, *, name: str, content_type: str, content: bytes
) -> dict[str, Any]:
    """Upload a file to GitHub's user attachments as the PR's selected account.

    GitHub keeps the file whether or not a description ends up linking to it.
    """
    with _gh_deadline_scope(time.monotonic() + _ATTACHMENT_REQUEST_SECONDS):
        host = _host_args(root, reference)
        token = _pr_token(root, reference)
        if _gh_timed_out.get():
            # The selected account's token is unknown, so the upload must not run as another.
            raise ValueError(_ATTACHMENT_TIMED_OUT)
        rc, out, _ = _gh(
            ["api", *host, f"repos/{reference.repository}", "--jq", ".id"],
            cwd=root,
            token=token,
        )
        repository_id = out.strip()
        if rc != 0 or not repository_id.isdigit():
            if _gh_timed_out.get():
                raise ValueError(_ATTACHMENT_TIMED_OUT)
            raise ValueError("GitHub could not find this pull request's repository.")
        query = (
            f"content_type={quote(content_type, safe='')}"
            f"&name={quote(name, safe='')}&repository_id={repository_id}"
        )
        with tempfile.NamedTemporaryFile(suffix=f"-{name}", delete_on_close=False) as upload:
            upload.write(content)
            upload.close()
            rc, out, err = _gh(
                [
                    "api",
                    "-X",
                    "POST",
                    f"{_attachments_endpoint(reference.host)}?{query}",
                    "-H",
                    "Content-Type: application/octet-stream",
                    "--input",
                    upload.name,
                ],
                cwd=root,
                token=token,
                timeout=_ATTACHMENT_REQUEST_SECONDS,
            )
        if _gh_timed_out.get():
            raise ValueError(_ATTACHMENT_TIMED_OUT)
    try:
        result = json.loads(out) if out.strip() else None
    except ValueError:
        result = None
    url = result.get("url") if isinstance(result, dict) else None
    if rc == 0 and isinstance(url, str) and _is_attachment_url(url, reference.host):
        return {
            "object": PR_ATTACHMENT_OBJECT,
            "url": url,
            "name": name,
            "content_type": content_type,
        }
    if re.search(r"\(HTTP 40[134]\)", err):
        raise ValueError(
            "The GitHub account on the host needs push access to attach files "
            "to this pull request."
        )
    errors = result.get("errors") if isinstance(result, dict) else None
    detail = next(
        (
            error["message"]
            for error in (errors if isinstance(errors, list) else [])
            if isinstance(error, dict) and isinstance(error.get("message"), str)
        ),
        None,
    )
    if detail and "(HTTP 422)" in err:
        raise ValueError(f"GitHub rejected the file: {detail}")
    raise ValueError("GitHub could not upload the file. Try again.")


# ── Chat link previews ───────────────────────────────────────────────────────
# A preview of a PR, issue, commit or Actions run named by a chat link. Callers
# pass validated parts, never a URL; every read is a ``gh`` call or, with no
# ``gh`` login, an unauthenticated read of GitHub's public API.

GITHUB_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
PREVIEW_REF = {
    "pull": re.compile(r"^[0-9]{1,12}$"),
    "issue": re.compile(r"^[0-9]{1,12}$"),
    "run": re.compile(r"^[0-9]{1,20}$"),
    "commit": re.compile(r"^[0-9a-fA-F]{7,40}$"),
}
_PREVIEW_TTL_SECONDS = 60.0
# Unauthenticated reads share GitHub's 60 requests an hour per address.
_PUBLIC_PREVIEW_TTL_SECONDS = 15 * 60.0
_PUBLIC_RATE_LIMIT_BACKOFF_SECONDS = 15 * 60.0
_PREVIEW_CACHE_MAX = 512
_STALE_FILE_SECONDS = 3600.0
_PUBLIC_API = "https://api.github.com"
_UNREADABLE_STATUSES = frozenset({403, 404, 410, 422})
_PASSING_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})
_PREVIEW_PR_FIELDS = "title,state,isDraft,labels,statusCheckRollup"

_preview_lock = threading.Lock()
_preview_cache: dict[tuple[str, ...], tuple[float, dict[str, Any] | None]] = {}
_public_blocked_until = 0.0


def _cache_directory() -> Path:
    return data_dir() / "github" / "read-cache"


def _cache_path(key: tuple[str, ...]) -> Path:
    digest = hashlib.sha256("\0".join(key).encode()).hexdigest()
    return _cache_directory() / f"{digest}.json"


def _cache_get(key: tuple[str, ...], *, shared: bool = False) -> tuple[bool, Any]:
    """Look ``key`` up in the runner's read cache: ``(hit, value)``.

    A ``shared`` entry also lives in a file under the data directory, since every
    session runs in its own runner process and only the disk is common to them. Memory
    keeps it for at most the viewer window, so a write another process made (which
    deletes the file) is seen within that window.
    """
    now = time.monotonic()
    with _preview_lock:
        cached = _preview_cache.get(key)
    if cached is not None and cached[0] > now:
        return True, cached[1]
    if not shared:
        return False, None
    try:
        entry = json.loads(_cache_path(key).read_text(encoding="utf-8"))
        remaining = float(entry["expires"]) - time.time()
        value = entry["value"]
    except (OSError, ValueError, KeyError, TypeError):
        return False, None
    if remaining <= 0:
        with suppress(OSError):
            _cache_path(key).unlink()
        return False, None
    with _preview_lock:
        _preview_cache[key] = (now + min(remaining, _OPEN_PR_TTL_SECONDS), value)
    return True, value


def _remove_unheld_lock(path: Path) -> None:
    """Delete a lock file only if nobody holds it (``flock`` doesn't touch its mtime)."""
    if fcntl is None:
        path.unlink()
        return
    descriptor = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path.unlink()
    except BlockingIOError:
        pass
    finally:
        os.close(descriptor)


def _sweep_cache_directory(directory: Path) -> None:
    """Remove expired entries and files a crashed writer or finished lock left behind."""
    now = time.time()
    entries = list(directory.glob("*.json"))
    for path in entries:
        try:
            if float(json.loads(path.read_text(encoding="utf-8"))["expires"]) <= now:
                path.unlink()
                continue
        except (OSError, ValueError, KeyError, TypeError):
            pass
    for path in (*directory.glob("*.tmp"), *(directory / "invalidated").glob("*")):
        with suppress(OSError):
            if now - path.stat().st_mtime > _STALE_FILE_SECONDS:
                path.unlink()
    for path in directory.glob("*.lock"):
        with suppress(OSError):
            if now - path.stat().st_mtime > _STALE_FILE_SECONDS:
                _remove_unheld_lock(path)
    remaining = list(directory.glob("*.json"))
    if len(remaining) >= _PREVIEW_CACHE_MAX:
        for path in remaining:
            with suppress(OSError):
                path.unlink()


def _cache_put(
    key: tuple[str, ...],
    value: Any,
    ttl: float,
    *,
    shared: bool = False,
    started: float | None = None,
) -> None:
    """Remember ``value`` for ``ttl`` seconds, in memory and, if ``shared``, on disk.

    A PR read that ``started`` before a write to that PR is not remembered: it may
    predate the write.
    """
    if started is not None and _invalidated_since(key) >= started:
        return
    with _preview_lock:
        if len(_preview_cache) >= _PREVIEW_CACHE_MAX:
            _preview_cache.clear()
        memory_ttl = min(ttl, _OPEN_PR_TTL_SECONDS) if shared else ttl
        _preview_cache[key] = (time.monotonic() + memory_ttl, value)
    if not shared:
        return
    path = _cache_path(key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _sweep_cache_directory(path.parent)
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"expires": time.time() + ttl, "key": list(key), "value": value}, handle)
        os.replace(temporary, path)
        if started is not None and _invalidated_since(key) >= started:
            path.unlink(missing_ok=True)
            with _preview_lock:
                _preview_cache.pop(key, None)
    except (OSError, TypeError, ValueError):
        _logger.debug("github_resource: could not share a cached read", exc_info=True)


class _PreviewReadError(Exception):
    """GitHub answered something other than a readable object or a refusal."""


def valid_preview_parts(owner: str, repo: str, kind: str, ref: str) -> bool:
    """Whether the parts name a PR, issue, commit or run with safe path segments."""
    ref_pattern = PREVIEW_REF.get(kind)
    return bool(
        ref_pattern
        and ref_pattern.fullmatch(ref)
        and all(GITHUB_NAME.fullmatch(name) and ".." not in name for name in (owner, repo))
    )


def _public_get(path: str) -> dict[str, Any] | None:
    """Read ``path`` from GitHub's API with no token; ``None`` when not readable.

    :raises _PreviewReadError: On rate limiting, which also pauses further public reads.
    """
    global _public_blocked_until
    if time.monotonic() < _public_blocked_until:
        raise _PreviewReadError("public GitHub API rate limit")
    try:
        response = httpx.get(
            _PUBLIC_API + path,
            headers={"Accept": "application/vnd.github+json"},
            timeout=_gh_timeout_seconds(),
        )
    except httpx.HTTPError as exc:
        raise _PreviewReadError(str(exc)) from exc
    if response.status_code == 429 or (
        response.status_code == 403 and response.headers.get("x-ratelimit-remaining") == "0"
    ):
        _public_blocked_until = time.monotonic() + _PUBLIC_RATE_LIMIT_BACKOFF_SECONDS
        raise _PreviewReadError("public GitHub API rate limit")
    if response.status_code in _UNREADABLE_STATUSES:
        return None
    if response.status_code != 200:
        raise _PreviewReadError(f"GitHub returned {response.status_code}")
    data = response.json()
    return data if isinstance(data, dict) else None


def _gh_api_get(root: str, path: str, token: str | None) -> dict[str, Any] | None:
    """Read ``path`` through ``gh api``; ``None`` when this login can't read it."""
    rc, out, err = _gh(["api", "--hostname", "github.com", path], cwd=root, token=token)
    if rc == 0:
        try:
            data = json.loads(out)
        except ValueError as exc:
            raise _PreviewReadError("unreadable gh output") from exc
        return data if isinstance(data, dict) else None
    status = re.search(r"\(HTTP (\d{3})\)", err)
    if status and int(status.group(1)) in _UNREADABLE_STATUSES:
        return None
    raise _PreviewReadError(err.strip()[:200] or "gh api failed")


def _rest_checks(get: Any, repo_path: str, sha: str) -> dict[str, int] | None:
    """Check-run and commit-status counts for ``sha``; ``None`` when it has none."""
    # ponytail: first 100 of each; page on if a repo runs more.
    runs = (get(f"{repo_path}/commits/{sha}/check-runs?per_page=100") or {}).get(
        "check_runs"
    ) or []
    statuses = (get(f"{repo_path}/commits/{sha}/status?per_page=100") or {}).get("statuses") or []
    if not runs and not statuses:
        return None
    counts = {"passed": 0, "failed": 0, "pending": 0}
    for run in runs:
        if run.get("status") != "completed":
            counts["pending"] += 1
        elif run.get("conclusion") in _PASSING_CONCLUSIONS:
            counts["passed"] += 1
        else:
            counts["failed"] += 1
    for status in statuses:
        state = status.get("state")
        counts[
            "passed" if state == "success" else "pending" if state == "pending" else "failed"
        ] += 1
    return counts


def _rest_pull_preview(get: Any, repo_path: str, number: str) -> dict[str, Any] | None:
    pull = get(f"{repo_path}/pulls/{number}")
    if pull is None:
        return None
    state = str(pull.get("state") or "")
    if pull.get("merged"):
        state = "merged"
    elif state == "open" and pull.get("draft"):
        state = "draft"
    head_sha = str((pull.get("head") or {}).get("sha") or "")
    return {
        "kind": "pull",
        "title": str(pull.get("title") or ""),
        "state": state,
        "labels": _shape_labels(pull.get("labels")),
        "checks": _rest_checks(get, repo_path, head_sha) if head_sha else None,
    }


def _pull_view_preview(data: dict[str, Any]) -> dict[str, Any]:
    """A PR preview from ``gh pr view --json``, with the panel's check summary."""
    state = str(data.get("state") or "").lower()
    if state == "open" and data.get("isDraft"):
        state = "draft"
    summary = _summarize_checks(data.get("statusCheckRollup"))
    checks = (
        {
            "passed": summary["passing"] + summary["skipped"],
            "failed": summary["failing"] + summary["cancelled"],
            "pending": summary["pending"],
        }
        if summary["total"]
        else None
    )
    return {
        "kind": "pull",
        "title": str(data.get("title") or ""),
        "state": state,
        "labels": _shape_labels(data.get("labels")),
        "checks": checks,
    }


def _rest_preview(get: Any, repo_path: str, kind: str, ref: str) -> dict[str, Any] | None:
    if kind == "issue":
        issue = get(f"{repo_path}/issues/{ref}")
        if issue is None:
            return None
        if issue.get("pull_request"):
            return _rest_pull_preview(get, repo_path, ref)
        closed_as = "not_planned" if issue.get("state_reason") == "not_planned" else ""
        preview: dict[str, Any] = {
            "kind": "issue",
            "title": str(issue.get("title") or ""),
            "state": closed_as or str(issue.get("state") or ""),
            "labels": _shape_labels(issue.get("labels")),
            "checks": None,
        }
        status = issue.get("status")
        if isinstance(status, dict) and status.get("name"):
            # GitHub's issue status (Backlog, In progress, ...); absent on repos without statuses.
            preview["status"] = {
                "name": str(status["name"]),
                "category": str(status.get("category") or ""),
            }
        return preview
    if kind == "pull":
        return _rest_pull_preview(get, repo_path, ref)
    if kind == "commit":
        commit = get(f"{repo_path}/commits/{ref}")
        if commit is None:
            return None
        message = str((commit.get("commit") or {}).get("message") or "")
        return {
            "kind": "commit",
            "title": message.split("\n", 1)[0],
            "state": None,
            "labels": [],
            "checks": _rest_checks(get, repo_path, str(commit.get("sha") or ref)),
        }
    run = get(f"{repo_path}/actions/runs/{ref}")
    if run is None:
        return None
    status = str(run.get("status") or "")
    return {
        "kind": "run",
        "title": str(run.get("display_title") or run.get("name") or ""),
        "state": str(run.get("conclusion") or status) if status == "completed" else status,
        "labels": [],
        "checks": None,
    }


def _read_identity(root: str, owner: str, repo: str) -> tuple[bool, str | None, str | None]:
    """Decide once who a link preview reads as: ``(signed_in, token, cache_identity)``.

    The accounts tell whether ``gh`` is signed in; :func:`_resolve_read` names the identity.
    When no credential can be bound, ``cache_identity`` is ``None``: don't cache.
    """
    authenticated, accounts = _cached_accounts(root)
    logins = [
        account
        for account in accounts
        if account.get("host") == "github.com" and account.get("state") == "success"
    ]
    if (accounts and not logins) or (not authenticated and not accounts):
        return False, None, ""
    token, identity = _resolve_read(
        root, _repo_token(root, f"github.com/{owner}/{repo}"), "github.com"
    )
    return True, token, identity


def _read_link_preview(
    root: str, owner: str, repo: str, kind: str, ref: str, *, signed_in: bool, token: str | None
) -> dict[str, Any] | None:
    repo_path = f"/repos/{owner}/{repo}"
    if not signed_in:
        return _rest_preview(_public_get, repo_path, kind, ref)

    def get(path: str) -> dict[str, Any] | None:
        return _gh_api_get(root, path, token)

    if kind == "pull":
        data = _gh_pr_view(
            [ref, "-R", f"github.com/{owner}/{repo}"], _PREVIEW_PR_FIELDS, cwd=root, token=token
        )
        return _pull_view_preview(data) if data is not None else None
    return _rest_preview(get, repo_path, kind, ref)


def github_link_preview(
    root: str, owner: str, repo: str, kind: str, ref: str
) -> dict[str, Any] | None:
    """Preview a GitHub PR, issue, commit or Actions run for a chat link.

    Reads through the host's ``gh`` login (as the repository's preferred account,
    like the PR panel), or without a token for a public repo when ``gh`` is signed
    out. Results are cached on the runner.

    :param root: The session workspace, used as ``gh``'s working directory.
    :returns: ``{kind, title, state, labels, checks}``, or ``None`` when ``gh`` is
        missing, the object is unreadable to this login, or GitHub can't be reached.
    :raises ValueError: When the parts are not valid.
    """
    if not valid_preview_parts(owner, repo, kind, ref):
        raise ValueError("Invalid preview reference")
    if not _gh_installed():
        return None
    signed_in, token, identity = _read_identity(root, owner, repo)
    key = (identity or "", owner.lower(), repo.lower(), kind, ref.lower())
    if identity is not None:
        hit, cached = _cache_get(key)
        if hit:
            return cached
    try:
        preview = _read_link_preview(
            root, owner, repo, kind, ref, signed_in=signed_in, token=token
        )
    except _PreviewReadError as exc:
        _logger.info("github_resource: link preview not read: %s", exc)
        return None
    ttl = _PREVIEW_TTL_SECONDS if signed_in else _PUBLIC_PREVIEW_TTL_SECONDS
    # An account switched during the read may have answered it: remember it only if
    # the identity is the same after.
    if identity is not None and _read_identity(root, owner, repo)[2] == identity:
        _cache_put(key, preview, ttl)
    return preview
