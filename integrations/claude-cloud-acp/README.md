# Claude Code cloud sessions as an Omnigent agent

**Status: experimental.** It relies on undocumented claude.ai endpoints, so it can break without notice; see Limits.

`claude_cloud_acp.py` is a stdio ACP server. When Omnigent runs it as an
`acp:<slug>` agent, the agent runs in a Claude Code cloud session
(claude.ai/code) on Anthropic's infrastructure and on your claude.ai plan,
while the conversation shows up in Omnigent.

| Omnigent | Cloud session |
|---|---|
| First prompt | `claude --cloud "<prompt>"` from the session cwd, on a pseudo-terminal, because the CLI creates cloud sessions only interactively (`--cloud` refuses `--print`). The bridge reads the new session id from the screen and closes the local view. The VM clones that checkout's GitHub remote and branch, or uploads a bundle when there is no GitHub remote |
| Model and effort | The bridge advertises `haiku`, `sonnet` and `opus` in `session/new.models`. Omnigent's pick (or the spec's `model`) arrives as `session/set_model` before the first prompt, and the bridge creates the cloud session with `claude --cloud … --model <pick>`; a full model id passes through too, and with no pick the account default applies. The CLI fixes the model at creation, so a later turn that picks a different model fails with a message saying so (Omnigent ignores a refusal of `set_model` itself). A session resumed after a bridge restart keeps the model it started on, with a note, since the original model isn't recorded. The model the cloud ran on shows as a `[cloud] model: …` thought line from the session's init event. Effort: the bridge advertises an `effort` config option (`low`, `medium`, `high`, `xhigh`, `max`, the levels `claude --effort` takes). Omnigent's `reasoning_effort` arrives as `session/set_config_option` before the first prompt and the bridge creates the cloud session with `--effort <level>`; any other level is refused before a cloud session exists, with a message naming the allowed levels, and a later turn that picks a different level fails like a different model does. `CLAUDE_CLOUD_EFFORT` (an `env_passthrough` entry) stays as the default for sessions that pick none; the cloud reports no effort back, so only the flag is verified, not the level it ran at |
| Later prompts | `POST /v1/code/sessions/{id}/events` with a `user` event, the call the CLI's own interactive client makes. Any process with the login can send, so nothing depends on the creating process staying alive |
| Cancel | The same endpoint with a `control_request` of subtype `interrupt`. The running tool is stopped and the turn ends with `error_during_execution`. Omnigent terminates the agent process right after `session/cancel`, so the bridge sends the interrupt at once, and finishes sending it when EOF or SIGTERM arrives first. Nothing else stops the cloud session: a bridge that is terminated without a cancel, or that loses the network, leaves the turn running |
| Resume | The first answer starts with a `cloud session: https://claude.ai/code/<id>` line, sent as soon as the session exists, so a turn that fails still leaves it in the transcript. A new bridge process that gets Omnigent's replayed transcript finds that line and sends only the latest message to the same cloud session. A respawned bridge prompted with an old ACP session id finds the cloud session in `$XDG_STATE_HOME/claude-cloud-acp/sessions.json`. A prompt that starts with that line adopts an existing cloud session |
| Catching up | A bridge that is new to its cloud session reads the session's event log before it sends. When the last message's turn was never mirrored to its end (it still runs, or it ended while no bridge was there), the bridge mirrors that turn first, from the message on. When that message is exactly the one it was asked to send (a retry after a failed turn), it sends nothing more, so the task does not run twice. A turn counts as seen only once its response has gone to Omnigent, so a bridge that dies just before is covered too. The same check runs in a bridge whose own last turn failed; it shows again what the failed turn had already read, because Omnigent drops what a failed turn showed. Control requests that were answered, or whose turn has ended, are not run again; one that still waits is answered, also when it came before the message (an MCP handshake the stopped bridge never answered). A tool call runs at most once on this host: its result is kept under `answers/` in the state directory until the cloud session has it and a day longer (a second bridge may have listed the request as open just before the answer arrived, and then sends the kept result), and a call that a bridge restart cut short, or whose kept result is incomplete, fails with a message that it may have taken effect |
| Cut-off turn | A cloud session that waits too long on the host (a tool call or a permission while the laptop sleeps) loses its sandbox. The next event starts a new CLI, which waits for a message, and the turn never gets a `result`. The bridge sees a new CLI start after the turn produced output, waits for 30 seconds in a row with nothing from the turn and no answer of its own under way, and then posts one note that asks the session to check what is done and continue. In a catch-up, a retried message is replaced by that note, and that is its one continuation. A second cut-off in the same turn fails the turn |
| Archived session | A send that gets `409 session_not_active` unarchives the session and resends. History comes back, but files and the repo checkout from before the archive are gone. An idle or stopped VM re-provisions by itself on the next send |
| Ending and naming | The bridge never archives on its own: Omnigent kills it after every cancel and runner restart, and sends nothing when a session is deleted, so it cannot tell an ended session from a live one. Whatever ends the Omnigent session runs `python3 claude_cloud_acp.py --archive <target>...` (`POST /v1/code/sessions/{id}/archive`; one already archived counts as done), and whatever names it can run `--title "<title>" <target>` (`PUT /v1/code/sessions/{id}` with the title, the CLI's own rename). A target is a cloud session id, its claude.ai URL, or `-` for an Omnigent transcript on stdin, whose last `cloud session:` line names the session. A target that names none fails the command before any call |
| Transcript | `GET /v1/code/sessions/{id}/events/stream` (server-sent events), from the sequence number of the posted message, reconnecting from the last event seen (after 1 s, doubling up to 30 s while the failures last); assistant text, thinking, tool calls and tool results become ACP `session/update`s; the `result` event ends the turn |
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
         cancel_on_abandon: false     # a turn that fails on this side leaves the cloud session running
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

An idle bridge exits after `CLAUDE_CLOUD_IDLE_EXIT_S` seconds (default 60, `0` keeps
it), taking its MCP server child with it, and a bridge whose runner is gone exits
at once. The next message starts a new bridge, which re-attaches the same cloud
session from the replayed transcript's `cloud session:` line.

Offline check of the event mapping: `python3 claude_cloud_acp.py --self-check`.

Live scenarios, each starting real cloud sessions: `python3 e2e.py
turns|restart|respawn|archived|cancel|teardown|detach|subagent <checkout>` and
`python3 e2e.py untrusted`. The
tapes in `demo/` record them with [VHS](https://github.com/charmbracelet/vhs):
`vhs demo/omnigent.tape -o "$tmp/omnigent.mp4"`, run from this directory. The tapes
read a trusted git checkout with a GitHub remote from `$DEMO_CHECKOUT`, and the agent
specs from `$DEMO_AGENT` and `$DEMO_AGENT_SANDBOXED` (the same spec without `sandbox`).

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
approval card unless the session runs in `bypassPermissions`: set it per session
with `permission_mode` on the session create request (`POST /v1/sessions`), or
for every session with `permission_mode: bypassPermissions` in the agent spec's
`executor.config`. Policy runs first in both, so a policy deny still denies.
A card left unanswered for 30 minutes denies the call. Tools the CLI treats as
read-only, such as `Read` and `Grep`, never ask.

## Limits

- **Policy:** the cloud CLI runs the VM's built-in tools (Bash, edits) without
  asking the client unless the repo's committed `.claude/settings.json` lists
  them under `permissions.ask`, or the CLI's own sensitive-file check fires (an
  edit to a dotfile config such as `.pre-commit-config.yaml`); that check asks
  whatever the rules say. With those rules, each call goes to Omnigent's TOOL_CALL
  policy and approval card (see [Policy](#policy)). `claude --cloud` accepts
  `--permission-mode`, but the VM still starts in `default` and its sensitive-file
  safety check still asks, so the bridge passes no mode. Under `bypassPermissions`,
  Omnigent answers that request (policy first), which is what removes those cards. The CLI does not apply SDK hooks
  (`initialize` answers `hooks_applied: false`), and user-level `~/.claude`
  settings do not reach the VM.
- **Undocumented API:** the events endpoints are not part of the documented
  public API, so they can change without notice. Sends go out unsigned and
  without a trusted-device token; an account whose sessions require either gets
  the send refused.
- **Host tools while the host is away:** a tool served by this host, or a
  permission request, cannot be answered while the laptop sleeps. Seen once:
  after 28 minutes of waiting, the cloud had dropped the sandbox and the turn.
  The bridge then continues the session (see Cut-off turn), and the agent
  repeats the tool call. Work that needs no host tool runs on.
- **Messages typed in claude.ai:** the bridge ends a turn at the first `result`
  after its message. A message someone types in the web UI while that turn runs
  is queued in the cloud and runs after it; Omnigent shows that queued turn only
  as part of the next message's catch-up.
- **Tool calls across hosts:** the at-most-once record is a file on this host.
  When the session moves to a bridge on another machine while a tool call waits,
  that bridge runs the call again.
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
  names the cloud session, which is left as it is. Only time the session could
  be reached counts: while this host sleeps or has no network the turn waits,
  says so in a thought, and catches up from the stream when the network is
  back. A stream the API refuses (an expired login) does count, and the error
  names the reason.
- **Server restarts:** the bridge runs under the host's runner, not the server,
  so a server restart leaves a running turn running and its output reaches the
  session once the runner reconnects. A host restart keeps it only with
  `host.keep_runners_on_restart: true`.
- **A turn Omnigent gives up on:** Omnigent sends `session/cancel` when a turn
  fails or times out on its side, which would interrupt the cloud session.
  `cancel_on_abandon: false` in the agent's config turns that off, so only
  Stop cancels. The session then shows a failed turn while the cloud session
  works on; the next message mirrors what it did.
- **While no bridge is attached:** a tool call to an MCP server on this host,
  or a permission request, waits in the cloud session until a bridge answers
  it. A request that the CLI gave up on, in a turn that still runs, is answered
  late once. A Stop that the network drops is sent again while the bridge
  lives, and is lost when the bridge is stopped first.
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
