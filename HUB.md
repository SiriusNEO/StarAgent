# Hub and Dashboard

The StarAgent Hub is the browser-facing control plane and also acts as the local Node. Browsers
talk only to the Hub; local tmux operations run on the Hub machine, while remote operations are
proxied to the Node that owns the Session.

For the default single-machine experience, run `staragent` instead. Launcher goes directly to the
local Agents catalog and reuses the exact Node workspace rendered by Hub after selecting a Node.
It does not poll or expose configured Remote Nodes.

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
runtime records; tmux remains the source of truth for live Sessions. Windows Desktop stores state in
`%LOCALAPPDATA%\StarAgent\state`.

## Dashboard Surfaces

- **Nodes** is the Dashboard landing page. It configures the machines reachable from the Hub,
  reports each connection state, and opens that Node's workspace.
- **Current Node** opens that machine's details, reported StarAgent version, official update controls,
  and runtime dependency manager.
- **Sessions**, after selecting a Node, exposes only that Node's lifecycle status and owns Create
  Session, conversation resume, tmux adoption, Chat, Terminal, and workspace browsing.
- **Agents**, after selecting a Node, inventories its coding CLIs, reports login and usage support,
  exposes managed update actions, lists launch presets, and discovers resumable conversations.
- **Logs**, after selecting a Node, queries that Node's centralized service-event archive. The local
  Node also provides the Hub service log as a separate source.
- **Lark** configures the optional notification and command integration described in [LARK.md](LARK.md).
- **Settings** owns language, appearance, and Gallery.

## Official Updates

Open a Node and select its **Current Node** card to check that machine's checkout against the matching
branch on `https://github.com/SiriusNEO/StarAgent`. The card also shows the version and commit reported
by the Node heartbeat. `main` follows the Stable channel and `dev` follows the Preview channel. Checks
run after the details page has rendered and never switch branches.

**Update now** asks the selected Node to fetch its own official branch and advance its checkout with
`git merge --ff-only`. The Hub cannot submit a remote, branch, commit, or shell command. Each Node
refuses to update a dirty working tree, a detached or unsupported branch, a diverged history, or an
`origin` that is not the official GitHub repository. It never resets or overwrites local work.

The local Node restarts only the supervised Hub Dashboard child; a Remote Node restarts only its
supervised `staragent-node` child. Existing tmux Agent Sessions keep running. An unsupervised service
reports that a manual restart is required. Older Nodes that do not report the `staragent_update`
capability remain usable and show a one-time terminal-upgrade instruction; after that bootstrap,
their future updates can be managed from the Hub.

## Runtime Dependencies

**Current Node** checks the selected machine rather than the Hub browser for StarAgent's supporting
tools. Source-installed Linux/macOS Nodes report tmux; self-contained Desktop Nodes report bundled
Native PTY (ConPTY on Windows). Every platform also reports the optional Tailscale CLI and the
optional Node.js/npm runtime used by npm-based Harness installation paths. Results are cached on the
Node for 60 seconds and can be refreshed explicitly.

For a missing tool, the page shows only installation methods backed by a package manager detected on
that Node, plus official documentation. Node.js also links to an npmmirror download for users in
China. The browser submits a dependency and option ID—not shell text—and the selected Node rebuilds
the reviewed argv before execution. Privileged background installs are non-interactive; if the
operating system needs an administrator password, copy the displayed command into the interactive
Terminal instead.

Installing the Tailscale package does not sign in, join a tailnet, enable SSH, or configure
`tailscale serve`; those network lifecycle steps remain user-owned. Merely opening Current Node never
installs or changes software.

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

For Codex, StarAgent also reads Codex's own bounded `version.json` update cache. A fresh cached
`latest_version` is compared with the installed CLI version: an exact or newer local version is
shown as up to date, its update button is suppressed, and the update API becomes a no-op. Missing or
stale cache data remains **unknown** rather than being presented as proof that an update exists.

### Skills Discovery

Each Harness detail page includes a read-only inventory of globally installed and preinstalled
Skills on the selected Node. StarAgent scans only directories derived from the Harness's effective
managed environment:

- Codex: `$CODEX_HOME/skills/.system`, `$CODEX_HOME/skills`, the global Agent Skills compatibility
  directory, and manifest-declared Skills from installed Codex plugins.
- Claude Code: `$CLAUDE_CONFIG_DIR/skills` plus enabled, user-scoped plugin installs recorded by
  Claude Code under its versioned plugin cache. This follows Claude Code's documented
  [Skills](https://code.claude.com/docs/en/slash-commands) and
  [plugin cache](https://code.claude.com/docs/en/plugins-reference) layout.
- OpenCode: `$OPENCODE_CONFIG_DIR/skills` plus its documented global `.claude/skills` and
  `.agents/skills` [compatibility locations](https://opencode.ai/docs/skills).

The Agents page is Node-scoped rather than project-scoped, so repository-local Skills are not mixed
into this inventory. A future Session-scoped view can add its known working directory without
opening an arbitrary-path scan API.

The walker skips nested symlinks and applies fixed root, entry, depth, file-count, and file-size
limits. It reads only YAML frontmatter and returns the Skill name, description, source class, and
modification time. `SKILL.md` instructions, body text, credentials, and resolved absolute paths are
never sent to Hub. The browser can request a cache refresh but cannot provide a path. Hub applies the
same allowlists and output bounds again to Remote Node payloads.

### Harness Terminal

Each Harness detail page can launch the selected Harness directly in a real interactive terminal on
the selected Node. It uses the same xterm.js, WebSocket, and PTY stack as a Session terminal,
including terminal control sequences, resizing, Ctrl+C, copy, and safe web links. When the Harness
exits, StarAgent drops into the service user's login Shell so diagnostics and updates remain possible.

This terminal is not a sandbox: commands have the same environment and operating-system permissions
as the StarAgent service user. The Shell starts in that user's home directory. Closing or leaving the
page terminates the Shell process group, and no persistent tmux Session is created. Hub proxies the
PTY only to the Node selected in the current workspace.

### Usage

For Codex, each machine can show live rate-limit windows, reset times, plan, available credits,
and reset count through the CLI's read-only local app-server status RPC. This does not send a model
request.

Claude Code does not expose an equivalent non-interactive quota response. StarAgent therefore
reports its authentication state and provides the interactive `/status` command instead of
estimating a percentage.

### Managed Updates

When a Harness is missing, StarAgent offers its official native installer, the npmjs registry, and
China-friendly npm routes through npmmirror and Tencent Cloud. Command-based routes can be copied;
supported Nodes can also run them after an explicit confirmation. Registry overrides apply only to
that install command and never rewrite the user's global npm configuration. npm routes require npm
to be present and never trigger a hidden Node.js installation.

The installation UI uses one selected source and one primary action. Alternative sources remain
visible, while reviewed command text stays collapsed until requested. Runtime dependency installers
use the same source-picker and confirmation flow, keeping installation progress and errors in the
page instead of browser alerts.

On Windows, Codex and Claude Code default to their official native PowerShell installers. OpenCode
defaults to its official Windows Release binary: StarAgent validates the GitHub release metadata,
download host, declared size, SHA-256 digest, and zip layout before atomically placing
`opencode.exe` in the user's `.opencode\bin` directory. No package manager is added by these native
routes.

For installed Harnesses, **Update now** also requires an explicit confirmation and runs only an
internal allowlisted argv derived from a freshly detected installation source. Both install and
update endpoints accept fixed action identifiers rather than browser-provided shell text. Inventory
checks never install or update software automatically.

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
