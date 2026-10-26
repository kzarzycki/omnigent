# Agent guidance

Guidance for AI agents (Claude Code, Copilot, Cursor, etc.) working in this
repository. See `CONTRIBUTING.md` for the full contributor workflow.

## Reuse and design

- Search for and reuse existing helpers before adding code. Refactor an existing
  implementation if that avoids duplication.
- Use descriptive names; avoid unexplained abbreviations and single-letter names.

## Test changes

Before adding test setup, read [the test guide](tests/README.md) and
[primitive catalog](tests/_helpers/README.md), then inspect the nearest scenario.
Reuse sufficient coverage unchanged or extend a compatible scenario. Keep input
matrices at the lowest reliable layer; retain E2E for distinct process, transport
or browser boundaries. Reuse primitives with explicit caller inputs and keep
faults and assertions in the test. Explain new setup or a separate E2E in the
PR's Test Plan (Repro uses `evidence`; Resolve uses `test_audit`). Count helper changes in
test LOC; fewer lines alone do not establish adequate coverage.

- Add a unit test for non-trivial functions, methods, and exported APIs. Add
  coverage when changed behavior needs it.
- Run the relevant tests before submitting. Document unrelated pre-existing
  failures in the PR description.

## Committing

Run the `pre-commit` hook before committing (`pre-commit run --all-files`, or
let it run on staged files via `git commit`). Fix any issues it reports so the
commit lands clean — CI runs the same checks.

## Local development shortcuts

Use `just` for common tasks; run `just --list` for grouped recipes.

- `just ensure` — install/check prerequisites
- `just run-ios` / `just run-android` — build/run mobile apps
- `just dev` / `just dev-mobile` — start the omnigent dev pod
- `just electron-dev` / `just electron-build` — Electron desktop shell
- `just lint` / `just lint-all` — run pre-commit
- `just normalize-locks` — rewrite lockfile registries to PyPI/npmjs.org

## Pull requests

When you open a pull request, fill in the repo's PR template at
`.github/pull_request_template.md` (case-sensitive on Linux — note the lowercase
filename). Keep every section and checkbox row so reviewers can skim them,
except the optional Changelog section as described below.

- **Summary** — what changed and why.
- **Test Plan** — how you verified it.
- **Demo** — a **video or images** showing the change. Expected on contributor
  PRs for UI / frontend changes (check the "UI / frontend change" box under
  *Type of change*) so reviewers can see the new behaviour without checking out
  the branch. Use `N/A` for non-visual changes.
- **Type of change** / **Test coverage** — check all that apply (at least one
  each).
- **Coverage notes** — required if you checked "Manual verification completed"
  or "Not applicable".
- **Release notes** — choose exactly one Yes/No checkbox and keep both rows.
  Choose Yes only for outstanding user-facing features, bug fixes, UX changes,
  and breaking changes. Breaking changes must choose Yes. Features behind a
  feature flag are eligible only once the flag is enabled for users. Choose No
  for small fixes or improvements, features behind disabled flags, and internal
  changes with no user impact. This is the author's recommendation for
  maintainers curating the release notes.
- **Changelog** — if Release notes is Yes, write one line describing the change
  for users, including compatibility impact for breaking changes. If No, delete
  this section; the complete changelog still credits the PR using its title.

Generate the description from the actual diff and this session's context — lead
with the motivation, then the change. Don't pass a `--body` that skips these
sections.

### Demo media

Do not commit screenshots or recordings created only as PR or issue evidence.
Upload them as GitHub attachments and embed the attachment URLs in the PR's
Demo section or issue comments. Keep local captures outside the tracked tree,
and redact private data before uploading.

Commit media only when it serves maintained documentation, product assets, or
test baselines, not merely to obtain a public demo URL. If attachment upload is
unavailable, explain the limitation and ask for help instead of committing the
files as a workaround.

## Finishing a task

When you finish a task, print instructions to the user on how to test it: the
commands to run, the inputs to provide, or the steps to reproduce so they can
verify the result themselves. Prefer verification that is best performed by a
human, such as concrete manual behavior checks, rather than only listing unit
test commands. Don't leave the user guessing how to confirm the work — tell
them exactly what to do.

To drive the app yourself and prove a user-facing change on every entry point a
user can reach, start with `feature-map/README.md`, then read the relevant maps and linked skills.

## Deprecating features

When deprecating a feature, note the version in which it is expected to be
removed so we can clean it up when that version ships. Call out the deprecation
version in code (e.g. a `@deprecated` tag or comment naming the target release)
and in the PR/commit description, so there's a clear marker to act on later.

## Code comments

Keep comments short and focused on the code, not on the change history.

- **Keep them brief** — prefer one or two lines. Avoid comments longer than
  three lines; if you need more, the code likely needs refactoring or a doc
  string, not a wall of inline commentary.
- **Describe the scenario, not the PR** — explain *what* the code handles or
  *why* it exists, in terms a future reader needs. Don't reference PR numbers,
  issue numbers, or ticket IDs (e.g. `#1646`, `fixes JIRA-123`); the scenario
  should be clear without chasing external links.

## Database query names

Application stores use `make_named_managed_session_maker` and give every
session a stable semantic operation name. The session-level name must describe
the caller's intent rather than repeat SQL syntax; use a nested
`query_name_scope` only when one transaction needs distinct names for important
subqueries. Because the named session covers implicit flush and commit, don't
add an explicit `flush()` only to make a query name observable.

## Framework-owned instructions

Keep runtime lifecycle and metadata instructions separate from portable agent
instructions:

- Agent-spec and per-request instructions are user-authored. Framework-owned
  instructions are additive runtime behavior and are appended after them in
  `omnigent/runtime/prompt.py`.
- Keep the canonical instruction text and lifecycle gate in the owning framework
  module. Harness adapters should only transport the composed instructions; do
  not duplicate policy across adapters or add lifecycle metadata to `AgentSpec`.
- If framework instructions grow beyond a small ordered list, introduce a
  structured `FrameworkInstructions` value at the prompt-composition boundary.

## This fork: landing PRs into `mine`

Open PRs against `mine`, the fork's default branch; `main` only mirrors upstream.

Never merge a PR into `mine` with GitHub's merge button. GitHub signs that
commit with its own key, so every machine's `omnigent-update` refuses to install
the head and the nightly sync refuses to take it. Land a PR on a landing host
with the engineering loop's `uv run <engineering-loop skill>/scripts/approvals.py
land <pr> --wait`, run from a checkout of the PR's head (its worktree): it reads
`docs/agents/loop.md` there, whose `land` setting makes it squash the PR into one
commit signed with this checkout's key, check it against `allowed_signers`, and
fast-forward `mine` to it. The fork runs no GitHub Actions (`loop.ci: false`), so
first record `mise run check:all` passing on the head with `approvals.py
local-ci <pr>` (run `mise trust` once in a new checkout).

A landing host has `gh` logged in to an account that can push to
`kzarzycki/omnigent`, and an SSH signing key of its own: its public key in
dotagents' `patches/omnigent/allowed_signers` and in every consumer's trust list
(each machine's `~/.config/omnigent/allowed_signers`, read by `omnigent-update`,
and the VPS's `/etc/omnigent/allowed_signers`, read by `omnigent-deploy`). The
checkout signs with it:

```sh
git config gpg.format ssh
git config user.signingkey ~/.ssh/git_signing_ed25519.pub
git config commit.gpgsign true
```

`land` refuses a checkout that doesn't sign with SSH, and a squash the
allowed signers file doesn't accept, before it pushes. Landing hosts today are the
bridge host (a Linux VPS) and a macOS build host, each with its own key in the
allowed signers file. Their names are machine-specific, so they stay out of this
repo: `.claude/skills/cloud-dev/fork-env.sh OMNIGENT_BRIDGE_HOST` and
`... OMNIGENT_MACOS_BUILD_HOST` print them from the local overlay
`~/.config/omnigent/fork-dev.conf`.

## This fork: developing in a Claude cloud session

Omnigent development runs in Claude cloud sessions (the `claude-cloud` agent,
environment `omnigent-dev`), so the load is off the owner's machines. In such a
session (`CLAUDE_CODE_REMOTE` is set), read `.claude/skills/cloud-dev/SKILL.md`
first. The VM can't reach GitHub's GraphQL API or hold a signing key, so every
`approvals.py` step, the signed land and the Codex verifier run on the bridge host,
through a host helper session the skill describes.

## This fork: filing upstream issues and PRs

Upstream's triage and duplicate-check bots comment within about a minute of a
new issue or PR. After filing, watch it for the first few minutes and answer any
question with evidence (for example `git merge-base --is-ancestor <fix>
upstream/main`) before reporting done.
