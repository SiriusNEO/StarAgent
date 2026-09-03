# Hub and Dashboard

The StarAgent Hub is the browser-facing control plane and also acts as the local Node. Browsers
talk only to the Hub; local tmux operations run on the Hub machine, while remote operations are
proxied to the Node that owns the Session.

## Run the Hub

```bash
pip install -e '.[dev]'
staragent hub --host 0.0.0.0 --port 8080
```

`staragent hub` creates the `staragent-hub` tmux system session by default. Open
`http://<hub-node>:8080` and log in with the token printed by the command.

## Authentication and State

Hub authentication is saved in `<staragent-source>/.staragent/auth_token`. Set
`STARAGENT_AUTH_TOKEN` before starting the Hub to choose the token yourself.

Runtime state defaults to `<staragent-source>/.staragent`. Set `STARAGENT_STATE_DIR` to move it.
The state directory contains Hub configuration, adoption metadata, centralized logs, and other
small runtime records; tmux remains the source of truth for live Sessions.

## Dashboard Surfaces

- **Nodes** is the Dashboard landing page. It configures the machines reachable from the Hub,
  reports each connection state, and opens that Node's workspace.
- **Sessions**, after selecting a Node, exposes only that Node's lifecycle status and owns Create
  Session, conversation resume, tmux adoption, Chat, Terminal, and workspace browsing.
- **Agents**, after selecting a Node, inventories its coding CLIs, reports login and usage support,
  exposes managed update actions, lists launch presets, and discovers resumable conversations.
- **Logs**, after selecting a Node, queries that Node's centralized service-event archive. The local
  Node also provides the Hub service log as a separate source.
- **Lark** configures the optional notification and command integration described in [LARK.md](LARK.md).

## Logs and Supervision

The Hub is the durable log authority. The Dashboard **Logs** page keeps Hub and per-Node service
events in one central archive. Rotating JSONL files live under:

- `.staragent/logs/hub.jsonl`
- `.staragent/logs/nodes/<node>.jsonl`

Remote Nodes keep only a bounded delivery outbox until the Hub pulls new events during heartbeat
refreshes. Older Nodes remain compatible because this exchange begins only when a Node advertises
log support.

Hub and Node services run behind a lightweight supervisor. Unexpected exits record the exit code,
uptime, recent process output, and automatic-restart event before the service is restarted with
bounded backoff. HTTP access URLs are not logged, and token-like values are redacted before
persistence.

## Agent CLI Inventory

The Dashboard **Agents** page checks Codex, Claude Code, and OpenCode inside each Node service's
`PATH`. Version checks run in parallel with short timeouts and a 60-second Node-side cache. A
**ready** result means only that the executable exists and its version command succeeds.

Login is checked separately without sending a model request:

- Codex uses its read-only login status command.
- Claude Code uses its read-only authentication status command.
- OpenCode reports whether provider credentials are configured.

Remote results are normalized before display. Account identity, raw credentials, and tokens are
not returned to the Hub.

### Usage

For Codex, each machine can show live rate-limit windows, reset times, plan, available credits,
and reset count through the CLI's read-only local app-server status RPC. This does not send a model
request.

Claude Code does not expose an equivalent non-interactive quota response. StarAgent therefore
reports its authentication state and provides the interactive `/status` command instead of
estimating a percentage.

### Managed Updates

StarAgent identifies common installation sources and offers copyable official install or update
commands. **Update now** always requires an explicit confirmation and runs only an internal
allowlisted argv derived from a freshly detected installation source. The browser cannot submit
arbitrary command text, and inventory checks never install or update software automatically.

The same optional update action is available in **Sessions → Create Session** before launch.

## Presets and Conversation Resume

One command-preset registry powers **Sessions → Create Session** and the read-only preset catalog
on the Agents page. General Session creation remains owned by Sessions.

Conversation history discovery is manual, bounded, and read-only. It scans only the configured
Codex and Claude Code history locations and returns normalized metadata plus short prompt previews;
source history files are never modified. A history card on Agents opens Create Session with its
Node, CLI, and conversation preselected, while Create Session can also scan directly.

The Hub combines the structured Agent and conversation ID with the selected command preset before
launch. Options such as Codex YOLO or Claude permission mode are retained, and older Remote Nodes
still receive an ordinary worker command.

## Related Documentation

- [ARCHITECTURE.md](ARCHITECTURE.md) — components, data flow, APIs, networking, and security model
- [SESSIONS.md](SESSIONS.md) — tmux ownership, lifecycle, and status detection
- [tailscale/README.md](tailscale/README.md) — Tailscale setup for Remote Nodes
- [LARK.md](LARK.md) — optional Lark integration
