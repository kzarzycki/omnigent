# Claude Code cloud sessions as an Omnigent agent

**Status: experimental.** It relies on undocumented claude.ai endpoints, so it can break without notice; see Limits.

`claude_cloud_acp.py` is a stdio ACP server. When Omnigent runs it as an
`acp:<slug>` agent, the agent runs in a Claude Code cloud session
(claude.ai/code) on Anthropic's infrastructure and on your claude.ai plan,
while the conversation shows up in Omnigent.

| Omnigent | Cloud session |
|---|---|
| First prompt | `claude --cloud "<prompt>"` from the session cwd, on a pseudo-terminal, because the CLI creates cloud sessions only interactively (`--cloud` refuses `--print`). The bridge reads the new session id from the screen and closes the local view. The VM clones that checkout's GitHub remote and branch, or uploads a bundle when there is no GitHub remote |
| Later prompts | `POST /v1/code/sessions/{id}/events` with a `user` event, the call the CLI's own interactive client makes. Any process with the login can send, so nothing depends on the creating process staying alive |
| Cancel | The same endpoint with a `control_request` of subtype `interrupt`. The running tool is stopped and the turn ends with `error_during_execution`. Omnigent terminates the agent process right after `session/cancel`, so the bridge sends the interrupt at once, and again on EOF or SIGTERM for any turn still running |
| Resume | The first answer ends with a `cloud session: https://claude.ai/code/<id>` line. A new bridge process that gets Omnigent's replayed transcript finds that line and sends only the latest message to the same cloud session. A respawned bridge prompted with an old ACP session id finds the cloud session in `$XDG_STATE_HOME/claude-cloud-acp/sessions.json`. A prompt that starts with that line adopts an existing cloud session |
| Archived session | A send that gets `409 session_not_active` unarchives the session and resends. History comes back, but files and the repo checkout from before the archive are gone. An idle or stopped VM re-provisions by itself on the next send |
| Transcript | `GET /v1/code/sessions/{id}/events`, polled; assistant text, thinking, tool calls and tool results become ACP `session/update`s; the `result` event ends the turn |
| Provisioning steps | `env_manager_log` lines, shown as thoughts |

The bridge stores no credential. Create goes through the `claude` CLI and its own
login. Sends and reads use that same login's access token, read from
where the CLI keeps it: the `Claude Code-credentials` keychain item for `$USER`
on macOS, or `~/.claude/.credentials.json`.

## Setup

1. Sign in with a claude.ai account that has cloud sessions: `claude auth login`.
2. Register the agent in `~/.omnigent/config.yaml`:

   ```yaml
   acp:
     agents:
       - name: Claude Cloud
         command: python3 /path/to/integrations/claude-cloud-acp/claude_cloud_acp.py
         omnigent_mcp: false          # the cloud VM cannot reach the local MCP relay
         inject_system_prompt: false  # Omnigent's prompt names local tools the VM lacks
   ```

3. Use an agent spec with harness `acp:claude-cloud` and no local sandbox. The
   bridge needs a pseudo-terminal, the keychain and `~/.claude`, and the
   default seatbelt sandbox blocks `/dev/ptmx`:

   ```yaml
   spec_version: 1
   name: claude-cloud
   executor:
     type: omnigent
     config:
       harness: acp:claude-cloud
   os_env:
     type: caller_process
     sandbox:
       type: none
   ```

4. Start the session with a git checkout as its workspace, because the bridge
   runs `claude --cloud` there. The cloud session gets the committed state of
   that checkout: a clone of its GitHub remote, or a seed bundle when there is
   none. Claude Code must already trust the folder (run `claude` there once).
   The bridge never answers the trust prompt and fails with that instruction
   instead.

Offline check of the event mapping: `python3 claude_cloud_acp.py --self-check`.

Live scenarios, each starting real cloud sessions: `python3 e2e.py
turns|restart|respawn|archived|cancel|teardown|subagent <checkout>` and
`python3 e2e.py untrusted`. The
tapes in `demo/` record them with [VHS](https://github.com/charmbracelet/vhs):
`vhs demo/omnigent.tape -o "$tmp/omnigent.mp4"`. The tapes expect the spike
checkout at `~/dev/ccr-spike-repo` and the agent specs at `~/dev/ccr-spike-agent`
and `~/dev/ccr-spike-agent-sandboxed` (the same spec without `sandbox`).

## Limits

- **Policy:** Omnigent policy and approval cards do not apply, since tools run
  in the VM. Cloud sessions run under the cloud environment's permission mode
  and network policy.
- **Undocumented API:** the events endpoints are not part of the documented
  public API, so they can change without notice. Sends go out unsigned and
  without a trusted-device token; an account whose sessions require either gets
  the send refused.
- **Polling:** updates arrive every `CLAUDE_CLOUD_POLL_S` seconds (default 2).
  `GET /v1/code/sessions/{id}/events/stream` serves the same events as
  server-sent events, resumable from a sequence number.
- **Sub-agents:** a sub-agent's steps appear inline, its text as thoughts and
  its tool calls titled `[sub-agent] <tool>`, not as a separate Omnigent session.
- **Forks (untested):** a forked Omnigent conversation carries the marker
  line, so both forks should drive the same cloud session.
- **Workspace:** a child session started with `sys_session_create` runs the
  bridge in the parent's workspace; when that is not a git checkout, the VM
  starts in `/home/user` with no repo.
- **Token lifetime:** only the default Claude config dir is supported. The
  bridge reads the CLI's access token but never refreshes it; the CLI does,
  whenever it runs. A token that expires with no `claude` run in between
  fails the next send or poll.

Environment overrides: `CLAUDE_CLOUD_CLI`, `CLAUDE_CLOUD_POLL_S`,
`CLAUDE_CLOUD_TURN_TIMEOUT_S`, `CLAUDE_CLOUD_TOKEN`, `CLAUDE_CLOUD_API_BASE`.
