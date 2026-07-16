# StarAgent Architecture

StarAgent is a tmux-first control plane for coding CLI sessions. The system treats tmux as the source of truth: if a session exists in tmux, StarAgent can observe it, attach to it, and optionally send chat input to it.

## Components

### Hub

The Hub is the web dashboard and coordinator.

- Runs with `staragent hub --host 0.0.0.0 --port 8080`.
- Serves the Dashboard UI.
- Stores lightweight state under `STARAGENT_STATE_DIR`, or `<staragent-source>/.staragent` by default.
- Knows which nodes exist and how to reach their StarAgent node API.
- Proxies remote node APIs and terminal WebSockets to the browser.

The Hub itself is also a tmux system session, usually named `staragent-hub`.

### Remote Node

A Remote Node is a machine that runs tmux sessions for coding work.

- Runs `staragent node --host 127.0.0.1 --port 8081`.
- Is usually supervised by a tmux system session named `staragent-node`.
- Exposes local tmux operations through HTTP and WebSocket APIs.
- Exposes cached Codex, Claude Code, and OpenCode executable and login checks to the Hub.
- Requires `STARAGENT_NODE_TOKEN` or `STARAGENT_AUTH_TOKEN` for all non-health APIs.
- Does not own dashboard state; it reports live local tmux state to the Hub.

The word `agent` is reserved for coding CLIs such as Codex, Claude, and OpenCode. A StarAgent node is infrastructure, not a coding agent session.

### Sessions

StarAgent has two kinds of sessions:

- `agent` sessions: interactive coding CLI sessions, such as Codex or Claude.
- `system` sessions: infrastructure sessions, such as `staragent-hub`, `staragent-node`, and `staragent-tailscaled`.

Agent sessions can be created from the Dashboard or adopted from existing tmux sessions. System sessions are visible for observability but are read-only from Chat.

The Agents page is the inventory and maintenance control plane for coding CLIs. A single preset
registry feeds both its read-only catalog and **Sessions → Create Session**. General session creation
remains on the Sessions page. Both surfaces can explicitly request an installed CLI update before a
Session starts; no update runs during inventory probing or page load. The same bounded conversation
history API feeds Agents discovery and the Create Session resume picker. The Agents page hands a selected
conversation to Create Session by URL; only Create Session owns the launch flow.

## Data Flow

```text
Browser
  |
  | HTTP / WebSocket
  v
StarAgent Hub :8080
  |
  | local tmux calls for local sessions
  | HTTP / WebSocket proxy for remote sessions
  v
StarAgent Node :8081
  |
  | tmux list/capture/send/attach
  v
tmux sessions
  |
  v
Codex / Claude / OpenCode / shell
```

The browser only talks to the Hub. For remote sessions, the Hub talks to the node endpoint that was added in the Nodes page.

## APIs

The Hub and Remote Node share the same core session operations:

- `GET /api/health`
- `GET /api/sessions`
- `GET /api/logs` (Remote Node outbox on Nodes; centralized archive query on the Hub)
- `GET /api/agent-tools` (Remote Node executable probe)
- `GET /api/nodes/{node}/agent-tools` (Hub view of a local or remote Node probe)
- `POST /api/agent-tools/{agent}/update` (Remote Node allowlisted CLI update)
- `POST /api/nodes/{node}/agent-tools/{agent}/update` (Hub update proxy)
- `GET /api/agent-history` (bounded, read-only Remote Node history scan)
- `GET /api/nodes/{node}/agent-history` (Hub proxy for an explicitly requested scan)
- `POST /api/workers`
- `POST /api/adopt`
- `DELETE /api/sessions/{name}`
- `POST /api/sessions/{name}/send`
- `GET /api/sessions/{name}/output`
- `GET /api/sessions/{name}/transcript-state`
- `WS /ws/sessions/{name}/terminal`

The Hub adds node management and browser authentication.

## Chat and Terminal

Terminal is a live tmux PTY view. It is the ground truth display and accepts direct keyboard input.

Chat is a structured view derived from each CLI's native transcript, with captured pane output as a fallback. StarAgent maps user and agent turns into the chat UI. Chat sends input through tmux, so messages also appear in the real terminal.

Session status uses the same agent-native lifecycle principle but a cheaper path: Codex and Claude JSONL files are scanned backward only until the newest user or completed-turn event. The Dashboard exposes only `idle`, `working`, and `review`; terminal attachment and tmux activity age do not affect these states. A completed turn remains `review` until its Session is viewed, while visible approval/input prompts remain actionable.

## Files

File browsing and preview are served from the machine that owns the session:

- Local session: Hub reads local files.
- Remote session: Hub proxies file APIs to the Remote Node.

Changed Files are derived from the session workspace Git status.

CLI inventory probes execute version commands and disable nonessential updater traffic. They cache
results on the Node and expose suggested update commands without executing them. An update begins
only after a separate authenticated POST and user confirmation. The Node re-probes the executable,
derives an argv list from the detected install source and an internal allowlist, and executes it
without a shell or interactive stdin. Per-Agent locks reject duplicate updates; output is bounded and
redacted, results are normalized at the Hub boundary, and success or failure is written to the
centralized log without command output. Login probes use
`codex login status`, `claude auth status --json`, and `opencode auth list`. The same cached probe uses Codex's local read-only
`account/rateLimits/read` app-server method for quota windows; Claude remaining usage is intentionally
left to its interactive `/status` command. No identity or credential values are returned, and all
remote payloads are normalized to an allowlisted shape before display. Conversation history scanning
is manual, reads only fixed Codex/Claude history locations, bounds both file count and bytes read, and
returns an allowlisted metadata shape with a short prompt preview. It never modifies source history
files or returns their paths. Resume creation sends a structured Agent/session ID to the Hub. The Hub
combines it with the selected preset before forwarding a normal worker command, so preset permissions
are retained and older Remote Nodes remain protocol-compatible.

## Logging and Supervision

The Hub is the durable log authority. Structured JSONL events rotate independently under:

- `.staragent/logs/hub.jsonl`
- `.staragent/logs/nodes/<node>.jsonl`

Each Remote Node keeps only a bounded `.staragent/log-outbox/node.jsonl` delivery buffer. A Node
advertises log support in its session response, then the Hub uses a persisted cursor to pull,
deduplicate, and archive new events during heartbeat refreshes. Older Nodes remain compatible
because the Hub only starts this extra exchange when the capability is advertised.

The `staragent-hub` and `staragent-node` tmux sessions contain a lightweight process supervisor.
It records process output and exit metadata, restarts an unexpectedly exited service with bounded
exponential backoff, and preserves the evidence that would otherwise disappear with a tmux pane.
Uvicorn access logging is disabled so WebSocket query credentials are not captured; structured
log fields are also redacted before they are written.

## Networking

StarAgent does not require a specific network provider. A node is just a reachable StarAgent node endpoint.

Supported practical layouts:

- Local only: Hub and sessions on the same machine.
- LAN: Hub reaches `http://<lan-ip>:8081`.
- Tailscale: Hub reaches `http://<100.x-ip>:8081` or a tailnet DNS name.

Tailscale helper scripts live under `tailscale/`, but StarAgent core logic only assumes the node endpoint is reachable.

## Security

The Dashboard is protected by `STARAGENT_AUTH_TOKEN`. Binding the Hub to a non-loopback address requires this token.

Remote node endpoints require a shared node token and should not be exposed directly to the public internet. Put them behind a private network layer such as LAN or Tailscale, and expose only the Hub dashboard to trusted users.
