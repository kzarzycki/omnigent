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
| Resume | The first answer starts with a `cloud session: https://claude.ai/code/<id>` line, sent as soon as the session exists, so a turn that fails still leaves it in the transcript. A new bridge process that gets Omnigent's replayed transcript finds that line and sends only the latest message to the same cloud session. A respawned bridge prompted with an old ACP session id finds the cloud session in `$XDG_STATE_HOME/claude-cloud-acp/sessions.json`. A prompt that starts with that line adopts an existing cloud session |
| Archived session | A send that gets `409 session_not_active` unarchives the session and resends. History comes back, but files and the repo checkout from before the archive are gone. An idle or stopped VM re-provisions by itself on the next send |
| Transcript | `GET /v1/code/sessions/{id}/events/stream` (server-sent events), from the sequence number of the posted message, reconnecting from the last event seen; assistant text, thinking, tool calls and tool results become ACP `session/update`s; the `result` event ends the turn |
| MCP servers | Each stdio server in `session/new.mcpServers`, Omnigent's own tools among them, runs on this host. The bridge registers it in the cloud session as an SDK MCP server (`mcp_set_servers`) and answers the CLI's `mcp_message` requests from that process. The traffic goes VM → Anthropic → bridge, so the VM needs no route to this host or the tailnet. A new bridge re-registers the servers on its first turn. Omnigent's `sys_os_*` tools are hidden: they act on this host, and the VM has its own shell and files. When servers are configured, a new session is created with a one-word bootstrap prompt, so the servers are registered before the real first prompt |
| Permission requests | A `can_use_tool` request for a tool of a served MCP server is allowed; Omnigent applies its own policy when its relay runs the tool. Any other goes to Omnigent as `session/request_permission`: policy, then an approval card |
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
         # omnigent_mcp defaults to true: Omnigent's tools are served from this host
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
checkout at `~/dev/ccr-spike-repo` and the agent specs at `~/dev/claude-cloud-agent`
and `~/dev/claude-cloud-agent-sandboxed` (the same spec without `sandbox`).

## Policy

Commit this to the repo the cloud session clones, so its built-in tools ask
Omnigent first:

```json
{
  "permissions": {
    "ask": ["Bash", "Write", "Edit", "MultiEdit", "NotebookEdit", "WebFetch"]
  }
}
```

The bridge forwards each `can_use_tool` request as `session/request_permission`.
Omnigent evaluates the agent's `tool_call` policies against the native tool
name (`Bash`, `Write`; the built-in policies already match them), then shows an
approval card unless the agent runs with `permission_mode: bypassPermissions`.
A card left unanswered for 30 minutes denies the call. Tools the CLI treats as
read-only, such as `Read` and `Grep`, never ask.

## Limits

- **Policy:** the cloud CLI runs the VM's built-in tools (Bash, edits) without
  asking the client unless the repo's committed `.claude/settings.json` lists
  them under `permissions.ask`; setting the permission mode from the client
  does not change that. With those rules, each call goes to Omnigent's TOOL_CALL
  policy and approval card (see [Policy](#policy)). The CLI does not apply SDK hooks
  (`initialize` answers `hooks_applied: false`), and user-level `~/.claude`
  settings do not reach the VM.
- **Undocumented API:** the events endpoints are not part of the documented
  public API, so they can change without notice. Sends go out unsigned and
  without a trusted-device token; an account whose sessions require either gets
  the send refused.
- **Bootstrap turn:** with MCP servers, each new cloud session's transcript
  starts with the bridge's `ready` exchange.
- **Child sessions:** an agent spec with `spawn: true` gives the cloud agent
  `sys_session_create` and `sys_session_send`; `config_path` is resolved on the
  Omnigent host.
- **Long tool calls:** while a turn waits on the cloud, the bridge sends an
  empty thought every 60 s, so Omnigent's 300 s idle timeout
  (`HARNESS_ACP_PROMPT_TIMEOUT_S`) never ends a turn whose tool call runs
  longer.
- **Stalled sessions:** a turn has no time limit while its cloud session shows
  life: any event on its stream (a `tool_progress` heartbeat comes every 90 s
  during a long `Bash` call), or a control request the bridge is still
  answering, such as a long Omnigent tool call or an approval card. After
  `CLAUDE_CLOUD_STALL_TIMEOUT_S` (30 minutes) with neither, the turn fails and
  names the cloud session, which is left as it is.
- **After a failed turn:** Omnigent drops a failed turn's output, `cloud session:`
  line included, and replays the transcript into a new bridge. The bridge
  records each cloud session it creates under the first prompt and working
  directory. A new bridge whose replay starts with exactly that prompt, in the
  same directory, continues the same cloud session rather than starting
  another.
- **Stream reader:** one per cloud session for the bridge's life, so control
  requests are answered between turns too. MCP requests to one server are
  handled one at a time.
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
  fails the next send or stream reconnect.

Environment overrides: `CLAUDE_CLOUD_CLI`,
`CLAUDE_CLOUD_STALL_TIMEOUT_S`, `CLAUDE_CLOUD_TOKEN`, `CLAUDE_CLOUD_API_BASE`.
