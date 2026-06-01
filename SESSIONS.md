# Session Model

StarAgent sessions are real tmux sessions. The dashboard never treats config files as source of truth.

## Types

- `agent`: a coding CLI session, such as Codex, Claude, Gemini, OpenCode, or a shell worker.
- `system`: infrastructure tmux sessions owned by StarAgent or networking helpers.

System sessions are long-running infrastructure processes. They are represented as tmux sessions so they can be inspected, restarted, and managed with the same tmux-first model as coding work.

Current system sessions:

- `staragent-hub`: runs the Hub dashboard on the main machine.
- `staragent-node`: runs the Remote Node API on a worker machine.
- `staragent-tailscaled`: runs userspace Tailscale when the machine has no systemd or TUN device.

System sessions are visible in the dashboard for observability. Chat is disabled because these sessions are not coding agents; use Terminal to inspect logs or attach with tmux when debugging infrastructure.

## Ownership

- Local session: tmux session on the Hub machine.
- Remote session: tmux session on a Remote Node, reached through the node API.

The browser only talks to the Hub. The Hub either acts on local tmux directly or proxies the request to the owning node.

## Lifecycle

Create Worker:

- Starts a new tmux session in a selected working directory.
- Runs the selected command, for example `codex --yolo`.
- The new session is an `agent` session.

Adopt Existing Tmux:

- Scans existing tmux panes for supported coding CLIs.
- Stores lightweight adoption metadata in `.staragent/adopted_sessions.json`.
- Keeps the original tmux session as the source of truth.

Stop:

- Stops created `agent` sessions.
- Adopted sessions are treated carefully in the UI because they existed before StarAgent.
- `system` sessions are read-only from Chat.

## Views

Terminal is the live tmux PTY view.

Chat is derived from the tmux transcript using CLI-specific parsers. Chat sends messages through tmux, so terminal output remains authoritative.
