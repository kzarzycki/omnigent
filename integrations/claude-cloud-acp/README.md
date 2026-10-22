# Claude Code cloud sessions as an Omnigent agent

`claude_cloud_acp.py` is a stdio ACP server. When Omnigent runs it as an
`acp:<slug>` agent, the agent runs in a Claude Code cloud session
(claude.ai/code) on Anthropic's infrastructure and on your claude.ai plan,
while the conversation shows up in Omnigent.

| Omnigent | Cloud session |
|---|---|
| First prompt | `claude --cloud "<prompt>"` from the session cwd, on a pseudo-terminal, because the CLI creates cloud sessions only interactively (`--cloud` refuses `--print`). The bridge reads the new session id from the screen and closes the local view. The VM clones that checkout's GitHub remote and branch, or uploads a bundle when there is no GitHub remote |
| Later prompts | `POST /v1/code/sessions/{id}/events` with a `user` event, the call the CLI's own interactive client makes. Any process with the login can send, so nothing depends on the creating process staying alive |
| Cancel | The same endpoint with a `control_request` of subtype `interrupt`. The running tool is stopped and the turn ends with `error_during_execution` |
| Transcript | `GET /v1/code/sessions/{id}/events`, polled; assistant text, thinking, tool calls and tool results become ACP `session/update`s; the `result` event ends the turn |
| Provisioning steps | `env_manager_log` lines, shown as thoughts |

The bridge stores no credential. Create and send go through the `claude` CLI and
its own login. The events read uses that same login's access token, read from
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
         omnigent_mcp: false   # the cloud VM cannot reach the local MCP relay
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
turns|restart|cancel|subagent <checkout>` and `python3 e2e.py untrusted`. The
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
- **Sub-agents:** a sub-agent shows as one `Agent` tool call with its final
  report; its own steps are dropped.
- **Resume:** the cloud session id lives only in the bridge process, and
  Omnigent's ACP executor neither calls `session/load` nor passes a
  conversation id, so a restarted Omnigent session starts a new cloud session.
- **Token lifetime:** only the default Claude config dir is supported. A
  single turn that runs longer than the access token's lifetime (the CLI
  refreshes it at the start of each turn) fails its poll.

Environment overrides: `CLAUDE_CLOUD_CLI`, `CLAUDE_CLOUD_POLL_S`,
`CLAUDE_CLOUD_TURN_TIMEOUT_S`, `CLAUDE_CLOUD_TOKEN`, `CLAUDE_CLOUD_API_BASE`.
