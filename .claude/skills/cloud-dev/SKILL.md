---
name: cloud-dev
description: How to develop this fork from a Claude cloud session (the `claude-cloud` agent, environment `omnigent-dev`) - what runs in the cloud VM, what goes through a host helper on the bridge host, and the VM's limits. Use at the start of any engineering-loop run in a Claude cloud session, and when changing the `omnigent-dev` environment.
---

# Developing in a Claude cloud session

Omnigent development runs in Claude cloud sessions through the `claude-cloud` Omnigent
agent, so the load is on Anthropic's machines rather than the owner's. The session's VM
(cloud machine) does the spec, the code, the tests and the PR. A few steps can't run
there and go through a host helper on the bridge host, where the bridge runs. The owner's
machine-specific values (host names, the environment's ID) stay out of this repo:
`fork-env.sh <NAME>` next to this file prints one, from the environment or the local
overlay `~/.config/omnigent/fork-dev.conf`, and fails naming the overlay when neither has it.

## The environment

- Sessions use the `omnigent-dev` cloud environment (its ID:
  `fork-env.sh OMNIGENT_DEV_ENVIRONMENT_ID`). It holds no secrets and no signing key:
  landing signs on a host.
- The host workspace a session starts from pins it in `.claude/settings.local.json`:
  `{"remote": {"defaultEnvironmentId": "<that ID>"}}`. Without that the
  session lands in the account's default environment, which lacks the tools below.
- `setup.sh` next to this file is the environment's setup script: `bubblewrap`,
  `openssh-client`, `pre-commit` and `mise`, logged to `/tmp/omnigent-dev-init.log`. It
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
- **Gate.** `mise trust -q && mise run check:all`, after the fetch above.
- **Tests.** A tool call stops at 10 minutes. On the VM's 4 CPUs:
  - `OMNIGENT_SKIP_WEB_UI=true uv sync --locked --extra all --group test`: about 7 minutes
    cold.
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

| Step | Why it can't run in the VM |
|---|---|
| Every `approvals.py` call: `check build`, `verdict`, `local-ci`, `land` | It reads PRs, issues, edit history and review threads through GraphQL |
| Resolving review threads | GraphQL only |
| The signed squash onto `mine` (inside `land`) | It signs with the host's key; the VM holds none |
| The Codex verifier | It is the other model family, and there is no Codex cloud bridge |
| Reading the verifier's report, writing your own report | Those files are on the host, where the chief of staff reads them |
| The bridge and the Omnigent MCP relay | They drive the cloud session from the host that holds the personal claude.ai login; Omnigent's tools reach the VM through them |
| Starting the lead (`start-lead.py`) and its workspace checkout | The chief of staff's side: the workspace names the environment and the branch the VM clones |
| Anything needing the tailnet, a local browser or Xcode | The VM has none of these. No Omnigent step needs them today |

- **Host helper.** Start one `claude-native-ui` child with `mcp__omnigent__sys_session_create`
  (model `claude-sonnet-5-5`, title `<story>-host`). It runs on the bridge host in your host
  workspace, the checkout your session started from. Send it exact commands and ask for
  their output and exit codes in its reply. It runs the engineering-loop scripts from the
  host's installed skill. `local-ci` and `land` check the checkout's HEAD against the PR
  head, so it runs both in a clean worktree of the PR head, after `mise trust` there
  (AGENTS.md, landing PRs into `mine`). It also writes your final report to the host path
  your brief names.
- **Verifier.** Have the helper make a detached worktree of the PR head on the host
  (`git fetch origin <branch> && git worktree add --detach <path> <sha>`). Start the
  verifier as a `codex-native-ui` child with `sys_session_create`. A child always starts
  in your host workspace, so its brief names that worktree and a report path inside it.
  The helper reads the report back to you and posts it with `approvals.py verdict`.
- **Closing children.** `sys_session_close` refuses a child a cloud session created (#110).
  Until that's fixed, the helper archives them with the orchestrating-agents skill's
  `omnigent_agent.py reap <session>…`.
