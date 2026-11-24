---
name: gatekeeper
description: Decide whether a change may take its next step - be built or merge - from proof on the forge and the team's policy in docs/agents/gatekeeper.md, and record the proofs (an approval, a verifier verdict, a check pass). Use when running a merge gate or required check, recording an approval, a verdict or a check pass, or landing a PR on its proofs.
metadata:
  requires: "gh uv"
---

# Gatekeeper

The gatekeeper answers one question: may this change take its next step? It reads the
proof on the forge (approval records, verifier reviews, check passes) and the team's policy,
and never decides on an agent's word, so its answer on a laptop is CI's answer.

Run `uv run <this skill's folder>/scripts/gatekeeper.py <verb>` anywhere in the checkout: it
reads `docs/agents/` at the checkout's root. `uv run` installs PyYAML, the one package it
needs beyond the standard library, from the script's inline header; a bare `python3` without
it exits with one line saying so. It needs Python 3.9 or later and `gh`, logged in, for GitHub.

| Verb | Does |
|---|---|
| `check build\|merge` | whether the change may be built, or merge |
| `record approval\|verdict\|checks` | records a person's approval, a verifier report as a review, or a check pass on the head |
| `land [--wait]` | every merge proof, then marks the PR ready and merges it, pinned to the head |
| `report [--json]` | reads a verifier report as the gatekeeper does; `--json` prints its facts |
| `delegate` | the delegated approver's `approved:merge`, feature branch only |

Exit codes, for every verb: `0` allowed or done; `1` refused, with one line per missing
proof; `3` waiting (pending checks, a forge state not computed yet): run it again. The
script's docstring is the manual: every proof each verb checks and every record it writes.

A PR carrying `needs-owner` or `loop:hold` is refused at merge, by `land` and `check merge` alike, with a line naming the
label: `loop:hold` is a lead's hold, or another coordinator's claim, and its PR comment says who and why.

## Files

- `scripts/gatekeeper.py`: the CLI, the policy reader and the gate.
- `scripts/github_adapter.py`: the GitHub adapter. The gatekeeper reaches the tracker, the
  forge and the evidence store only through ports, each served by the adapter the policy's
  `adapters` section names; GitHub's is the only one, and `gatekeeper.py` loads it from
  beside itself.
- `scripts/folding.py`: builds and reads the folded comments the gatekeeper and the loop post.
- `scripts/events.py` and [events.md](events.md): a step's state written as a small git ref
  (`emit`, `read`, `wait`, `clear`), which the gatekeeper reads and the loop and a runner write.
  events.md has the kinds, states, beat intervals and `event.json` fields; read it to write or
  read an event.
- `scripts/acceptance.py`: runs one spec's acceptance checks from a folder of executables, and
  the gatekeeper's `check` reads the evidence it leaves.
- [policy.md](policy.md): every policy key, its type and default, the rules the policy
  states, the batching queue, carry-over of a merge of main, the opt-out, and
  issue-tracker.md's settings block. Read it when writing or changing a project's policy, or
  when a refusal names a key.
- [breakglass.md](breakglass.md): the owner's break-glass land of a head with a red required check, its proof and
  the owner-side bypass setup. Read it when a required check blocks a PR the owner wants landed anyway.
- `scripts/approvals.py`: the gatekeeper's former name, kept for one release line. It runs
  `gatekeeper.py` with today's verbs (`approve`, `verdict`, `local-ci`), so a project's
  `approvals.py` command keeps working.

## Policy

The policy is the fenced `yaml` block under `## Policy` in the project's
`docs/agents/gatekeeper.md`, and only the gatekeeper reads it. Without that file it reads the
same block under `docs/agents/loop.md`'s `## Settings`, with a deprecation line, and it
refuses a project with both, since two sources of the bar would drift. It refuses, naming
the key, a section or key it doesn't know and a value of the wrong type, so a mistyped
setting is never read as a rule nobody wrote. [policy.md](policy.md) has every key, with an
example block, and `SCHEMA` in `gatekeeper.py` is the same list as code. Label names come
from `docs/agents/issue-tracker.md`'s settings block (policy.md, Tracker settings).

## Verifier report

`record verdict` posts a verifier's report as one PR review on the commit it names, and
`report [--json]` reads it the same way, so a report is written once, for people, in this
shape (anything else in it is prose the review carries):

- near the top, `Verifier: <family>, pass <n>`, such as `Verifier: codex/terra-6.1/med, pass
  5`. A lower tier (`mid` or `small`) is a word of its own after the family, `Verifier:
  codex/terra-6.1/med mid, pass 1`: `check merge` refuses a high-risk change whose newest
  verdict names one, and reads a line without one as the default tier. Text after the pass
  number (`(merge of main)`) goes into the review's first line;
- per finding, a heading `### <id> (<blocker|major|minor>) <path>:<line>: <title>`, its body
  up to the next heading outside a code fence. Any other `### ` heading is a sub-heading;
- at the end, `Head: <sha>`, the full commit reviewed, then `VERDICT: <n> blocker, <n>
  major, <n> minor`, `SATISFIED: yes|no` and `Risk: low — <reason>` or `Risk: high —
  <reason>`.

It refuses a report without the `Head:`, `Verifier:` or `Risk:` line, a `### ` heading with a
`(<severity>)` it can't read, and finding headings that don't add up to the `VERDICT:`
counts. A question for the owner goes in `<report>.question` beside it, which `report --json`
prints.

## Known-red tests

A test that fails for a reason unrelated to a PR turns the required check red for every PR, so it
gets a skip marker that links its issue, with the issue filed in the same change: pytest's `skip`
or `xfail(run=False)` with `#<issue>` in the reason, or the project's runner's equivalent. An
expected-failures list keeps the test running and its cost on every PR, so the gatekeeper takes
none: a project's entries become skips. A project's repo check refuses a skip or `xfail` whose
reason links no issue. A skip for the environment (a missing tool) needs no issue.

## The cap

The cap is 3 passes, unless the project's gatekeeper.md sets its own with `cap` under `loop`
in its policy. It is the last verifier pass that runs on its own: a head at it with a blocker
or major open lands only under the lead's recorded `Cap:` decision, which `check merge` reads.
`gatekeeper.py` reads the default from this line, so this is the one place to change it.
