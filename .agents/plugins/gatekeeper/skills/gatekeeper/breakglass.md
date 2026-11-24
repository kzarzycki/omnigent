# Break-glass land

A way to land one exact head whose required check is red or missing for a reason that is not the change: a flaky or
broken suite, or CI infrastructure down. The owner approves the head, `land` verifies the proof from GitHub's run data,
and **the owner merges it in the GitHub UI**: `land` never merges a break-glass PR. Read this when a required check
blocks a PR the owner wants landed anyway, or when writing the proof.

## The verbs

- `approvals.py breakglass-proof <pr> --head <sha> --failed-run <id> [--rerun <id>] [--smoke <id>] [--earlier <id>...]`:
  any writer or agent posts the proof comment. It names run ids only. The owner's record pins its hash, so an edit voids
  the approval.
- `approvals.py approve breakglass <pr> --head <full-sha> --by owner --reason <line> [--expires <ISO time>]`: the owner's
  word. It writes the `breakglass` commit status and an `Approved: breakglass` record (`By`, `Head`, `Proof`, `Expires`,
  `Reason`). `--by delegate` is refused, and `loop.delegate: all` never covers it. `--withdraw` revokes it. A push moves
  the head and voids it; the carry-over of a go (same tree, merge of base) does not apply.
- `check merge`: where a required check is red or missing and the record is current, it prints
  `required check `qualify` red; break-glass approved by the owner on <sha>` and does not hold on that check. Every other
  proof (spec, verifier, a `risk:high` go) is still required. With no record its output is unchanged.

The approval counts only when `loop.owner` (the policy) names the login that created the status and wrote the comment.
Without `loop.owner` no login is the owner's, and every record is refused.

## What land verifies

`land` reads each item from GitHub's runs (`GET /actions/runs/{id}`, its jobs and its artifacts), never from the proof
comment's words, which only name the run ids. A run counts only in this repo, completed, and within `breakglass.max_age` of
its `completed_at`; all but the failing run's own must be of the workflow that produces the required check.

- **The failure on this head**: the failing run on the exact head and the test ids it failed, from its `failed-tests`
  artifact (`{"failed": [...], "selected": [...]}`; `tools/failed_tests.py` makes it from pytest's JUnit file, and this
  repo's `qualify` uploads it). A failure with no test ids and a job that shows CI down (the runner lost, cancelled, timed out) is `infra`: only the smoke run and the
  earlier runs apply, and the owner is shown it. With no test ids and no job down, the failure is the change's own and is refused.
- **Classification**: each failed test against completed runs of the workflow on `main`'s recent heads and the PR's merge
  base, within `max_commits` of the head. It is `not caused by this change` when it also failed there, else `unexplained`,
  which refuses: only the change can explain it. The classification and the run links go in the proof comment.
- **Re-run alone**: a green run on the exact head whose `selected` list holds every failed test (`gh workflow run` with
  `tests` set, on the workflow that has that input). Not asked for `infra`.
- **Smoke**: a local-ci record for the head's tree whose command is `breakglass.smoke`, exit 0; or a green full Actions run on the head (`--smoke <run id>`).
- **Full run**: a smoke or earlier run counts only if it ran the whole required check: of the workflow that produces it,
  started by a `pull_request`, `push` or `schedule`, never a `workflow_dispatch` (its `tests` input can empty or narrow it),
  with a green job of each required check. Only the re-run alone is a dispatch.
- **Earlier green runs**: `breakglass.earlier_runs` green full runs of the workflow on recent heads that are ancestors of the head
  or of the base.

`breakglass-proof` runs this check before it posts, so a gap is said before the owner is asked; `check merge` and `land`
run it again on every call. A verified PR is left unmerged: `land` removes `loop:queued` and turns a `land` success on the
head to an error (a Mergify queue would merge on it), posts a `Break-glass verified` comment with the proof's link and the
trailer to paste into the squash message, and ends `needs-owner`. After the owner's merge, `land <pr>` writes the
`Break-glass land` comment (head, merge commit, the red or missing checks with run links, record and proof links).

## What the pack's helpers refuse

Agents run as the same Admin account as the owner, so the owner's bypass is theirs unless the pack's code refuses it. It
does, and this is the only thing that keeps the bypass the owner's:

- `gh()` in `github_adapter.py`, which every call of the pack goes through, refuses `gh pr merge` with the bypass flag and a
  call to the REST merge route (`PUT pulls/N/merge`) that sends any field but `merge_method`, `sha`, `commit_title` and
  `commit_message`, or a body from `--input`.
- The adapter's `merge` (the `gh pr merge` wrapper and its REST fallback) refuses any merge while a required check is not
  green on the head, red, pending or missing, whether or not a break-glass record exists.
- `land`'s merge step (GitHub's merge and the signed squash alike) refuses the same, and never merges a PR whose proof it
  verified (above).
- No script of the pack contains the bypass flag: a grep-level test asserts it.

## Settings

`breakglass` in the policy (policy.md): `max_age` (`7d`), `max_commits` (`20`), `smoke` (the gate command), `earlier_runs`
(`2`), `artifact` (`failed-tests`).

## Owner-side setup

The owner's merge needs the Admin repository role as a bypass actor, `bypass_mode: pull_request`, on each managed repo's
landing ruleset (mirrored in the repo's ruleset file where one exists). The owner adds it on approval; no PR of the pack
edits a ruleset. Agents act as the same Admin account, so the pack's own code refuses every bypass merge (below).
