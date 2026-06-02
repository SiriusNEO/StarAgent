# StarAgent Architecture

StarAgent is a tmux-first control plane for coding CLI sessions. The system treats tmux as the source of truth: if a session exists in tmux, StarAgent can observe it, attach to it, and optionally send chat input to it.

## Components

### Hub

The Hub is the web dashboard and coordinator.

- Runs with `staragent hub --host 0.0.0.0 --port 8080`.
- Serves the Dashboard UI.
- Stores lightweight state under `STARAGENT_STATE_DIR`, or `~/.local/state/staragent` by default.
- Knows which nodes exist and how to reach their StarAgent node API.
- Proxies remote node APIs and terminal WebSockets to the browser.

The Hub itself is also a tmux system session, usually named `staragent-hub`.

### Remote Node

A Remote Node is a machine that runs tmux sessions for coding work.

- Runs `staragent node --host 127.0.0.1 --port 8081`.
- Is usually supervised by a tmux system session named `staragent-node`.
- Exposes local tmux operations through HTTP and WebSocket APIs.
- Requires `STARAGENT_NODE_TOKEN` or `STARAGENT_AUTH_TOKEN` for all non-health APIs.
- Does not own dashboard state; it reports live local tmux state to the Hub.

The word `agent` is reserved for coding CLIs such as Codex, Claude, Gemini, and OpenCode. A StarAgent node is infrastructure, not a coding agent session.

### Sessions

StarAgent has two kinds of sessions:

- `agent` sessions: interactive coding CLI sessions, such as Codex or Claude.
- `system` sessions: infrastructure sessions, such as `staragent-hub`, `staragent-node`, and `staragent-tailscaled`.

Agent sessions can be created from the Dashboard or adopted from existing tmux sessions. System sessions are visible for observability but are read-only from Chat.

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
Codex / Claude / Gemini / shell
```

The browser only talks to the Hub. For remote sessions, the Hub talks to the node endpoint that was added in the Nodes page.

## APIs

The Hub and Remote Node share the same core session operations:

- `GET /api/health`
- `GET /api/sessions`
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

Chat is a structured view derived from the tmux transcript. StarAgent parses the captured pane output with CLI-specific transcript parsers, then maps user and agent turns into the chat UI. Chat sends input through tmux, so messages also appear in the real terminal.

## Files

File browsing and preview are served from the machine that owns the session:

- Local session: Hub reads local files.
- Remote session: Hub proxies file APIs to the Remote Node.

Changed Files are derived from the session workspace Git status.

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
