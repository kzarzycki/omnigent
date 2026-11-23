---
name: cloud-dev
description: How to develop this fork from a Claude cloud session (the `claude-cloud` agent, environment `omnigent-dev`) - what runs in the cloud VM, what goes through a host helper on the landing host, and the VM's limits. Use at the start of any engineering-loop run in a Claude cloud session, and when changing the `omnigent-dev` environment.
---

# Developing in a Claude cloud session

Omnigent development runs in Claude cloud sessions through the `claude-cloud` Omnigent
agent, so the load is on Anthropic's machines rather than the owner's. The session's VM
(cloud machine) does the spec, the code, the tests and the PR. A few steps can't run
there and go through a host helper on the landing host. Two roles: a **bridge host** is any
host that bridges cloud sessions (a lead may be bridged on any of them); the **landing host**
is the one with `gh`, the agent-skills checkout, signing and the report paths. They can be
the same machine. The owner's
machine-specific values (host names, the environment's ID) stay out of this repo:
`fork-env.sh <NAME>` next to this file prints one, from the environment or the local
overlay `~/.config/omnigent/fork-dev.conf`, and fails naming the overlay when neither has it.

## The environment

- Sessions use the `omnigent-dev` cloud environment (its ID:
  `fork-env.sh OMNIGENT_DEV_ENVIRONMENT_ID`). It holds no secrets.
- The host workspace a session starts from pins it in `.claude/settings.local.json`:
  `{"remote": {"defaultEnvironmentId": "<that ID>"}}`. Without that the
  session lands in the account's default environment, which lacks the tools below.
- `setup.sh` next to this file is the environment's setup script: first a `uv` that
  satisfies `uv.toml`'s `required-version` (the image's is older, so every uv command
  refuses to run; setup installs one from PyPI, puts `~/.local/bin` first on `PATH` and at the top
  of `~/.bashrc`, and leaves a satisfying uv alone), then `bubblewrap`,
  `openssh-client`, `pre-commit` and `mise`, plus a bounded warm-up of uv's cache for the
  Gate's sync, logged to `/tmp/omnigent-dev-init.log`, whose `done` line names the uv version. It
  exits 0 even when an install fails, since a failed setup script fails the whole session
  and the bridge leaves the Omnigent turn hanging; the log says `FAILED` instead. The
  environment keeps its own copy, so after changing the file, apply it from a host with
  the `claude` login: `POST /v1/environment_providers/<id>` with the environment's
  `name`, `description` and `config`, `config.init_script` set to the file's content
  (the call `.claude/skills/fork-sync/cloud-env.py` makes to create one, under the same
  headers).

## In the VM

- **Setup.** First `cat /tmp/omnigent-dev-init.log`. A `FAILED` line names the tools
  the setup script couldn't install; stop and report it rather than work without them.
- **Branch.** The session pushes only to its own `claude/…` branch, which becomes the
  PR's head. Its clone is shallow and has no `mine`: start with
  `git fetch origin mine && git reset --hard origin/mine`.
- **GitHub.** GraphQL answers 403, so `gh pr …` and `gh issue …` fail. Use REST through
  `gh api`, which works for reads and writes:
  - draft PR: `gh api repos/kzarzycki/omnigent/pulls -f title=… -f head=<branch> -f base=mine -F draft=true -f body=…`
  - comments, labels, issue edits: the `issues/<n>/…` REST routes.
- **Gate.** `mise trust -q && OMNIGENT_SKIP_WEB_UI=true uv sync --locked --extra all --group test --group lint && pnpm install --frozen-lockfile --filter web --filter ./editors/vscode && mise run check:all`,
  after the fetch above and after your first commit: its pre-commit covers
  `merge-base origin/mine..HEAD`, so before a commit it checks no files. The sync takes
  seconds once setup's warm-up has filled uv's cache, and minutes when it didn't finish. The `pnpm install` gives the JS hooks
  (`vscode-tsc`, `web-tsc`, ...) their `node_modules`; without it they exit 1 silently on a
  branch that touches the pnpm lockfile.
- **Tests.** A tool call stops at 10 minutes. On the VM's 4 CPUs:
  - `OMNIGENT_SKIP_WEB_UI=true uv sync --locked --extra all --group test --group lint`:
    seconds after setup's warm-up; cold, from 30 s to over 6 minutes, depending on the
    network. Without `--group lint` pre-commit's ruff hooks fail with "No module named ruff".
  - pytest with `-n auto --dist=loadfile`, one directory per call: `tests/server` about
    6 minutes; `tests/inner` and `tests/harnesses` under 2 each.
  - vitest, all of `web/`: about 8 minutes. Run it alone, or in shards when a run nears
    the limit.
  - `tests/harnesses/claude_native/test_claude_native_delivery_logging.py::test_delivery_diagnostics`
    has a 0.06 s budget the VM misses; it passes on a host. Don't count it against a PR.
  - Playwright: the preinstalled Chromium doesn't match the repo's Playwright build;
    launch it with `executable_path="/opt/pw-browsers/chromium"`.
- **Workers** run as your native sub-agents in the VM, so they share your checkout and
  branch. A cloud child session would clone the host workspace's branch instead.
- **Loop role files.** The engineering loop's skill isn't installed in the VM. Read it from
  a clone: `git clone -q --depth 1 https://github.com/kzarzycki/agent-skills /tmp/agent-skills`,
  then `/tmp/agent-skills/engineering/skills/engineering-loop/`.

## On the host

The engineering loop's scripts on the landing host are in its agent-skills checkout, not an
installed skill (`~/.claude/skills` has none, and any other copy on the host may be stale
and unable to sign a land). `fork-env.sh OMNIGENT_AGENT_SKILLS` prints that checkout; `approvals.py` is
`gatekeeper/skills/gatekeeper/scripts/approvals.py` under it.

Checked in a VM against pack `engineering-v0.35.1` (`approvals.py check merge <pr>`, `record verdict --help`,
`record checks --help`): the pack reads GitHub through REST where GraphQL answers 403, so the gatekeeper steps run in
the VM. Run them from the pack clone (Loop role files above; add `--branch engineering-v0.35.1` to pin it), as
`gatekeeper/skills/gatekeeper/scripts/approvals.py`.

**Runs in the VM:** `check build`, `check merge`, `record verdict` and `land`. `land` merges nothing: it posts the
`land` status on the gated head and adds `loop:queued`, and Mergify merges (a squash, or a merge commit for a `sync/*`
head). The `gate` check GitHub Actions runs on Mergify's draft of the merge is the proof, so no `record checks` run comes first.
A PR's review threads are read through the proxy's `ccr/review_threads` route.
Run `land` from a clean worktree of the PR head, after the setup steps below.

**Still needs the host:**

| Step | Why it can't run in the VM |
|---|---|
| Setting the board's `Risk` field | GraphQL only. `check build` and `check merge` say so and go on; the `risk:high` label alone shows the risk |
| Resolving review threads | GraphQL only (reading them works) |
| The Codex verifier | It is the other model family, and there is no Codex cloud bridge |
| Reading the verifier's report, writing your own report | Those files are on the host, where the chief of staff reads them |
| The bridge and the Omnigent MCP relay | They drive the cloud session from the host that holds the personal claude.ai login; Omnigent's tools reach the VM through them |
| Starting the lead (`start-lead.py`) and its workspace checkout | The chief of staff's side: the workspace names the environment and the branch the VM clones |
| Anything needing the tailnet, a local browser or Xcode | The VM has none of these. No Omnigent step needs them today |

- **Host helper.** For the steps in the table, start one `claude-native-ui` child with `mcp__omnigent__sys_session_create`
  (model `claude-sonnet-5-5`, title `<story>-host`). Create it on the landing host by naming
  it (`placement='host'`, `host_id`, `workspace`), whichever host bridges your session;
  a child with no host runs on the bridge host, which may lack `gh` and the report paths.
  It runs in your host workspace, the checkout your session started from. Send it exact commands and ask for
  their output and exit codes in its reply. It also writes your final report to the host path your brief names.
  A gatekeeper step run on the host (`land`) checks the checkout's HEAD against the PR head, so it
  runs in a clean worktree of the PR head, after these there (AGENTS.md, landing PRs into `mine`):
  `mise trust -q`, `OMNIGENT_SKIP_WEB_UI=true uv sync --locked --group lint` and
  `pnpm install --frozen-lockfile --filter web --filter ./editors/vscode` (the session-list
  lint is TypeScript, and the VS Code extension's type check runs once a branch touches the
  pnpm lockfile). Without them `land` stops without checking anything.
- **Keep host waits short.** A wait longer than the VM's idle window resumes the VM in a
  new sandbox without the Omnigent tools (#116), and a full host `tests/server` run takes
  about 24 minutes. Run long suites in the VM; send the helper steps that finish in a few
  minutes. `land` itself is quick, but `--wait` also waits out the queue's `gate` run, so send plain `land`
  and wait on the merge from the VM with `wait-for --pr <n> --until merged`.
- **Wake-ups.** A wake from a GitHub notification comes outside an Omnigent turn, where
  `sys_read_inbox` answers `runner_turn_context_desync`. End the turn and wait for the
  child's finish notice instead.
- **Verifier.** Have the helper make a detached worktree of the PR head on the landing host
  (`git fetch origin <branch> && git worktree add --detach <path> <sha>`). Start the
  verifier as a `codex-native-ui` child with `sys_session_create`. A child always starts
  in your host workspace, so its brief names that worktree and a report path inside it.
  The helper reads the report back to you and posts it with `approvals.py verdict`: the report file is on the host, which is why this step runs there, not in the VM.
- **Closing children.** `sys_session_close` closes a child a cloud session created with
  `sys_session_create`: it tombstones the child's free-form title and archives it.
