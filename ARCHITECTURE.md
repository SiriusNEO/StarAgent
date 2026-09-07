# StarAgent Architecture

StarAgent is a terminal-native control plane for coding CLI sessions. Source and server installs treat
tmux as the source of truth. Self-contained Desktop installs use an in-process native terminal
registry instead (ConPTY on Windows and PTY on Linux/macOS), without changing the Dashboard API.

## Components

### Launcher

Launcher is the default, single-Node StarAgent surface.

- Runs by invoking `staragent` with no subcommand.
- Opens the local Harness catalog by default.
- Reuses Hub's Node Workspace for the local Node, including Current Node details, Agents, Sessions,
  Logs, and runtime dependencies.
- Does not render the Nodes inventory or run Remote Node heartbeats.
- Opens a browser only for an attended local desktop launch; SSH and `--no-open` launches only print
  the URL.

Launcher and Hub are modes of one Dashboard application, not separate frontend implementations.
`create_app(mode="launcher")` is also the desktop host boundary: the native app owns the packaged
ASGI sidecar and window lifecycle while continuing to use the same runtime and web assets.

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
- `system` sessions: infrastructure sessions, such as `staragent-launcher`, `staragent-hub`,
  `staragent-node`, and `staragent-tailscaled`.

Agent sessions can be created from the Dashboard or adopted from existing tmux sessions. Desktop
Sessions use the bundled native registry and cannot adopt an unrelated system tmux session. System
sessions are visible for observability but are read-only from Chat.

Hub starts with the Nodes connection inventory. Selecting a Node scopes the Agents,
Sessions, and Logs pages to that machine; the browser never mixes multiple Nodes in one operational
view. Launcher skips the inventory and binds that same scope to the local Node. The selected Node's
Agents page is the inventory and maintenance control plane for coding CLIs.
A single preset registry feeds both its read-only catalog and **Sessions → Create Session**. General
session creation remains on the Sessions page. Both surfaces can explicitly request an installed CLI
update before a Session starts; no update runs during inventory probing or page load. The same bounded
conversation history API feeds Agents discovery and the Create Session resume picker. The Agents page
hands a selected conversation to Create Session by URL; only Create Session owns the launch flow. A
Harness detail page can also launch that Harness directly in a real interactive PTY on the selected
Node. It uses the same xterm.js and WebSocket path as a Session terminal. When the Harness exits it
falls back to a login Shell, but the process remains tied to the browser connection rather than a
persistent tmux Session.

The Harness status surface keeps account management and launch-profile selection together. Model catalogs are
resolved on the owning Node: Codex uses its local app-server `model/list`, OpenCode uses its fixed
`models` argv, and Claude Code combines its stable aliases with the configured `availableModels`
allowlist. Exact custom model IDs remain available for private providers and gateways. StarAgent
stores only per-Harness model and reasoning preferences in Node-local state. For new Sessions and
Harness test terminals it maps those values to validated `--model` plus vendor-native launch
overrides: Codex `--config model_reasoning_effort=…`, Claude Code `--effort`, or OpenCode
`--variant`. Codex effort choices and defaults come from each app-server model entry; Claude and
OpenCode choices follow their installed CLI/config capabilities. StarAgent does not rewrite vendor
TOML or JSONC, override explicit command arguments, change a running Session, or replace the launch
profile remembered by a resumed conversation.

Missing Harnesses expose a normalized catalog of reviewed install options: official native and npm
routes plus npmmirror and Tencent Cloud npm routes for China. The browser sends only an option ID.
Each Node resolves that ID back to fixed argv or a fixed official HTTPS installer before execution;
per-command registry arguments do not mutate npm's global configuration. Windows recommends the
official native PowerShell installers for Codex and Claude Code. OpenCode is installed directly from
its official Windows Release archive after StarAgent validates the asset metadata, size, redirect
host, archive layout, and GitHub SHA-256 digest. npm routes remain explicit fallbacks and require an
existing npm; StarAgent does not install or embed a second package manager.

Current Node owns the supporting-runtime inventory. The local or selected Remote Node detects its
terminal backend, Tailscale CLI, and Node.js/npm, then exposes only installation options for package
managers present on that machine. The browser sends fixed dependency and option identifiers; the Node
reconstructs allowlisted argv and returns bounded, redacted output. Installing Tailscale stops at the
package boundary and does not manage authentication, routes, or service configuration.

Each Harness also owns a read-only Skills inventory. The Node derives a fixed set of global, bundled,
compatibility, and enabled-plugin roots from that Harness's managed environment. A bounded walker
reads only `SKILL.md` frontmatter and returns normalized names, descriptions, scopes, and source
labels. The API accepts a Harness name and refresh flag, never a filesystem path; file bodies and
resolved absolute paths do not cross the Node boundary. Hub normalizes Remote Node results again
before sending them to the browser.

## Data Flow

```text
Browser / desktop shell
  |
  | HTTP / WebSocket
  v
Shared StarAgent Dashboard :8080
  |                                |
  | Launcher: local backend calls  | Hub: authenticated HTTP / WebSocket proxy
  v                                v
tmux or native PTY sessions  Remote Node :8081
                                   |
                                   | tmux list/capture/send/attach
                                   v
                              remote tmux sessions
```

Launcher terminates every operation on the local machine. In Hub mode, the browser still talks only
to Hub; Hub either operates on its local tmux server or proxies to the Node selected in the inventory.

## APIs

The Hub and Remote Node share the same core session operations:

- `GET /api/health`
- `GET /api/sessions`
- `GET /api/logs` (Remote Node outbox on Nodes; centralized archive query on the Hub)
- `GET /api/agent-tools` (Remote Node executable probe)
- `GET /api/nodes/{node}/agent-tools` (Hub view of a local or remote Node probe)
- `GET /api/agent-tools/{agent}/skills` (bounded, read-only Remote Node Skills inventory)
- `GET /api/nodes/{node}/agent-tools/{agent}/skills` (Hub view of the selected Node inventory)
- `GET /api/agent-tools/{agent}/models` (Node-local model catalog and launch preference)
- `GET /api/nodes/{node}/agent-tools/{agent}/models` (Hub view of the selected Node model catalog)
- `PUT /api/agent-tools/{agent}/models/preference` (Node-local model/reasoning launch preferences)
- `PUT /api/nodes/{node}/agent-tools/{agent}/models/preference` (Hub launch preference proxy)
- `GET /api/dependencies` (Remote Node supporting-runtime probe)
- `GET /api/nodes/{node}/dependencies` (Hub view of the selected Node's dependencies)
- `POST /api/dependencies/{dependency}/install/{option}` (Remote Node allowlisted install)
- `POST /api/nodes/{node}/dependencies/{dependency}/install/{option}` (Hub install proxy)
- `POST /api/agent-tools/{agent}/install/{option}` (Remote Node allowlisted CLI install)
- `POST /api/nodes/{node}/agent-tools/{agent}/install/{option}` (Hub install proxy)
- `POST /api/agent-tools/{agent}/update` (Remote Node allowlisted CLI update)
- `POST /api/nodes/{node}/agent-tools/{agent}/update` (Hub update proxy)
- `POST /api/agent-tools/{agent}/auth/logout` (Remote Node allowlisted noninteractive logout)
- `POST /api/nodes/{node}/agent-tools/{agent}/auth/logout` (Hub logout proxy)
- `POST /api/agent-tools/codex/auth/login/api-key` (Remote Node Codex stdin login)
- `POST /api/nodes/{node}/agent-tools/codex/auth/login/api-key` (Hub Codex login proxy)
- `WS /ws/agent-tools/{agent}/terminal` (Remote Node interactive Shell PTY)
- `WS /ws/nodes/{node}/agent-tools/{agent}/terminal` (Hub Shell proxy for the selected Node)
- `WS /ws/agent-tools/{agent}/auth/{action}/{method}` (Remote Node allowlisted auth PTY)
- `WS /ws/nodes/{node}/agent-tools/{agent}/auth/{action}/{method}` (Hub auth PTY proxy)
- `GET /api/agent-history` (bounded, read-only Remote Node history scan)
- `GET /api/nodes/{node}/agent-history` (Hub proxy for an explicitly requested scan)
- `GET /api/staragent-update` (Node-local official checkout status)
- `POST /api/staragent-update/check` and `POST /api/staragent-update/apply`
- `GET /api/nodes/{node}/staragent-update` (Hub view of the selected Node updater)
- `POST /api/nodes/{node}/staragent-update/check` and `POST /api/nodes/{node}/staragent-update/apply`
- `POST /api/workers`
- `POST /api/adopt`
- `DELETE /api/sessions/{name}`
- `POST /api/sessions/{name}/send`
- `GET /api/sessions/{name}/output`
- `GET /api/sessions/{name}/transcript-state`
- `WS /ws/sessions/{name}/terminal`

The Hub adds node management, browser authentication, and checkout maintenance:

- `GET /api/settings/update` (local cached Git state)
- `POST /api/settings/update/check` (fetch the matching official branch)
- `POST /api/settings/update/apply` (verified fast-forward plus supervised Dashboard restart)

## Chat and Terminal

Terminal is a live PTY view attached to tmux or the Desktop native registry. It is the ground truth
display and accepts direct keyboard input.

Chat is a structured view derived from each CLI's native transcript, with captured terminal output as
a fallback. StarAgent maps user and agent turns into the chat UI and sends input through the selected
session backend, so messages also appear in the real terminal.

Session status uses the same agent-native lifecycle principle but a cheaper path: Codex and Claude JSONL files are scanned backward only until the newest user or completed-turn event. The Dashboard exposes only `idle`, `working`, and `review`; terminal attachment and tmux activity age do not affect these states. A completed turn remains `review` until its Session is viewed, while visible approval/input prompts remain actionable.

## Files

File browsing and preview are served from the machine that owns the session:

- Local session: Hub reads local files.
- Remote session: Hub proxies file APIs to the Remote Node.

Changed Files are derived from the session workspace Git status.

CLI inventory probes execute version commands and disable nonessential updater traffic. They cache
results on the Node and expose reviewed install or update options without executing them. An install
or update begins only after a separate authenticated POST and user confirmation. The Node resolves a
fixed install option or derives an update argv from the detected installation source, then executes it
without a shell or interactive stdin. Official native installers are bounded downloads from fixed
HTTPS hosts and run from temporary files. Windows `.cmd` launchers are resolved explicitly and run
through `cmd.exe`, so missing prerequisites become a bounded diagnostic instead of an unhandled
`WinError 2`. Per-Agent locks reject duplicate maintenance operations. Output is bounded and
redacted, results are normalized at the Hub boundary, and success or failure is written to the
centralized log without command output.
Stored-login probes use
`codex login status`, `claude auth status --json`, and `opencode auth list`; Claude's effective managed
API-key, bearer-token, OAuth-token, profile, or cloud-provider environment takes precedence and is
reported only by variable name. The same cached probe uses Codex's local read-only
`account/rateLimits/read` app-server method for quota windows; Claude remaining usage is intentionally
left to its interactive `/status` command. No identity or credential values are returned, and all
remote payloads are normalized to an allowlisted shape before display.

Authentication actions use a fixed per-Harness registry rather than browser-provided commands. Codex
offers its browser and device-code PTY flows, API-key login, and a direct link to managed environment
credentials for custom providers; the OpenAI API key travels only in a no-store request body and
process stdin, never argv, environment, or structured logs. Claude Code exposes its
Claude account, Anthropic Console, and organization SSO PTY flows; Anthropic API keys and the supported
Bedrock, Vertex AI, or Foundry switches use the existing managed Harness environment. OpenCode exposes
its interactive provider add/remove pickers plus managed provider environment variables. Direct logout
is allowlisted for Codex and Claude Code; OpenCode provider removal remains interactive so the user can
choose exactly which credential source to delete. The legacy Codex device-login WebSocket stays mapped
for older Remote Nodes, while capability `agent_auth_management: 2` identifies the generic flow API.

Model and reasoning selection follow the same Node boundary. Catalog commands are fixed by the backend, bounded by
timeouts and normalized again at the Hub. Browser input is accepted only as a length- and
character-validated model or effort/variant ID, then stored in `harness-models.json` with user-only
permissions. The launch layer appends values only to recognized interactive Harness commands;
explicit model/reasoning arguments, administrative subcommands, and resume commands take precedence
and remain unchanged.

Conversation history scanning is manual, reads only fixed Codex/Claude history locations, bounds both
file count and bytes read, and returns an allowlisted metadata shape with a short prompt preview. It
never modifies source history files or returns their paths. Resume creation sends a structured
Agent/session ID to the Hub. The Hub combines it with the selected preset before forwarding a normal
worker command, so preset permissions are retained and older Remote Nodes remain protocol-compatible.

Each service reports process-cached StarAgent version, branch, and commit metadata in the
authenticated Node heartbeat; public health checks do not expose versions, and heartbeats do not
run Git or contact GitHub. Numeric capabilities, rather than exact package-version equality, gate
optional behavior so older Nodes remain usable.

The shared updater accepts only `main` or `dev` checkouts whose `origin` resolves to the official
GitHub repository. It fetches a fixed branch ref, rejects local changes and diverged history, and
uses `git merge --ff-only` against the verified commit hash. It never accepts a remote, branch,
commit, or command from the browser. A successful local-Node update terminates only the supervised
Dashboard child; a Remote-Node update terminates only the supervised Node child. tmux Agent Sessions
are unaffected.

The native desktop updater is a separate signed-binary path. Its connection window selects one of two
compiled-in endpoints: the latest normal Release for Stable, or the rolling `nightly` prerelease for
Nightly. Relevant `dev` pushes generate a monotonic prerelease SemVer, embed the full build commit, and
sign each updater package. CI uploads uniquely named packages before replacing `latest.json`, then
removes obsolete assets, so an interrupted rolling publish leaves either the old complete update or
the new complete update available. The unprivileged Dashboard WebView cannot select an endpoint or
invoke the updater.

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
