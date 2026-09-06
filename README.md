<h1 align="center">StarAgent</h1>

<p align="center">
  <img src="assets/logo.png" alt="StarAgent logo" width="256">
</p>

<p align="center">
  <a href="README.zh-CN.md">简体中文</a>
</p>

> ⚠️ This project is currently intended for personal use and is under active development. A stable version will be released later.

> ⚠️ This project was primarily built with vibe coding and may contain potential bugs. Please keep this in mind before using it.

StarAgent is a local-first **Agent Harness Launcher** and an optional multi-Node Hub for managing coding agent sessions. It reflects my own best practices for using multiple agents:

> **We need a lightweight persistent terminal wrapper for Codex / Claude Code that supports cross-machine connections and can be accessed from any device.**

## Design Principles

It is built around a few practical needs that show up when using coding agents day to day:

- We often run multiple agent CLI instances in parallel across different working directories, each handling a separate task. We need one place to check their status and interact with them in real time.
- We want to interact with our agents anywhere, anytime, and on any device. And the sessions should be consistent.
- Agent CLI sessions should be long-lived, so we do not need to keep typing `/resume`.

Based on hands-on experience, StarAgent uses the simplest effective stack for this workflow, making it feel like managing a small team of coding agents:

- **Platform-native persistent terminals**. Windows Desktop hosts sessions with ConPTY; Linux, macOS, and server Nodes use long-lived tmux sessions. See [SESSIONS.md](SESSIONS.md) for the session model.

- **Cross-machine connectivity via Tailscale**. Tailscale provides a secure and unified network layer across machines. See [tailscale/README.md](tailscale/README.md) for the Tailscale setup.

- **Unified management through a web dashboard**. The web dashboard lets you control agents from any device with a browser, including phones and laptops, without installing extra software.

Running `staragent` opens the single-Node `StarAgent Launcher`, scoped directly to the current machine. When cross-machine management is needed, `staragent hub` keeps the existing Hub experience: choose a Node, then enter the same Node workspace used by Launcher.
For the technical architecture, see [ARCHITECTURE.md](ARCHITECTURE.md).

## Preview

The screenshots below use sanitized demo data. The anime-inspired background is optional —
upload any image from the theme menu, then pair it with a color theme and glass surfaces.

Select a connected Node, then manage that machine's sessions in a focused workspace:

![StarAgent Sessions workspace with an anime-inspired observatory background](assets/demo-sessions-anime.webp)

Each session includes a lightweight chat console for interacting with agents, plus a Terminal and File Explorer.
Session detail pages keep an IM-style switcher on the left for moving between that Node's sessions without
returning to the session table; on narrow screens it collapses into a drawer.

![StarAgent session chat and locked PTY terminal](assets/demo-session-anime.webp)

Inspect Agent CLI availability, login state, update paths, and launch presets on the selected Node:

![StarAgent Agents dashboard with Codex, Claude Code, and OpenCode](assets/demo-agents-anime.webp)

**NOTICE:** On Linux/macOS Nodes, none of this gets in the way of SSHing into the server and attaching to the corresponding tmux session. On Windows Desktop, the bundled runtime owns the native ConPTY session. In both cases, closing a browser or workspace view only detaches the view; it does not stop the Agent session.

## Launcher

Install from a checkout, then start StarAgent with no subcommand:

```bash
pip install -e .
staragent
```

Launcher starts in the supervised `staragent-launcher` tmux system session and opens the local
Harness workspace at `http://127.0.0.1:8080`. Under SSH it prints the URL without trying to open a
browser. Use `staragent --no-open` to explicitly disable browser handoff.

Launcher is the normal single-machine experience. It opens directly on the local Agents catalog;
Sessions, Logs, Settings, and Current Node details use the same views as a Node selected in Hub.

## Desktop

A Tauri desktop app is available for Windows, Linux, and macOS. It can start the local Launcher or
connect to an existing Hub. The Windows package includes the StarAgent Python runtime and uses the
native Windows ConPTY backend for terminals and persistent Sessions—WSL, Python, and tmux are not
required.

### Download a prebuilt package

Open the [latest GitHub Release](https://github.com/SiriusNEO/StarAgent/releases/latest), expand
**Assets**, and download the package for your system:

| System | Release asset | Install |
| --- | --- | --- |
| Windows x64 | `*-setup.exe` | Run the per-user installer |
| Linux x64 | `*.AppImage` | Make it executable and run it |
| Debian / Ubuntu x64 | `*.deb` | Run `sudo apt install ./<downloaded-file>.deb` |
| macOS Intel / Apple Silicon | `*.dmg` | Open the universal DMG and drag StarAgent to Applications |

Do not confuse GitHub's automatically generated **Source code** archives with desktop installers. If
a release contains only those archives, it predates desktop packaging; use a newer release, a CI
artifact, or the source installation above. `v0.1.1` and earlier do not contain desktop installers.

Updater-enabled desktop releases offer **Stable** and **Nightly** channels and check the selected one
at startup. Stable follows normal Releases; Nightly follows signed builds from relevant `dev` commits.
A prompt shows the version, source commit, and release notes, and installation only starts after you
confirm **Update & restart**. If your current installation predates the updater, install the first
updater-enabled release manually once.

The same files can be downloaded with the [GitHub CLI](https://cli.github.com/):

```bash
# Linux AppImage example; use '*.deb' or '*.dmg' on the corresponding system.
gh release download --repo SiriusNEO/StarAgent --pattern '*.AppImage'
```

For an unreleased development build, select Nightly in the app. Individual build artifacts remain in
the [Desktop workflow](https://github.com/SiriusNEO/StarAgent/actions/workflows/desktop.yml) as
`staragent-windows-x64`, `staragent-linux-x64`, and `staragent-macos-universal`; GitHub requires
sign-in to download workflow artifacts directly.

Release updater payloads carry a Tauri update signature, while operating-system publisher signing is
still pending and may show an unknown-publisher warning. Windows local mode is self-contained; Agent
Harness CLIs remain optional and can be installed from the Agents page through their native Windows
install paths without adding Node.js/npm. npm and China-friendly mirrors remain explicit fallbacks
for systems that already provide npm.
Linux and macOS local mode still use the system StarAgent CLI and tmux. See
[DESKTOP.md](DESKTOP.md) for runtime details, updates, native builds, and signing status.

## Hub

Run this on the machine that runs the dashboard:

```bash
pip install -e .
staragent hub --host 0.0.0.0 --port 8080
```

`staragent hub` creates the `staragent-hub` tmux system session by default.
Open `http://<hub-node>:8080` and log in with the token printed by `staragent hub`.

See [HUB.md](HUB.md) for authentication and state settings, Dashboard surfaces, centralized logs,
Agent CLI checks, China-friendly install sources, updates, usage reporting, presets, and conversation
resume behavior.

## Remote Node

Run this on each machine that should run agent sessions:

```bash
pip install -e '.[dev]'
export STARAGENT_NODE_TOKEN="<same token as the Hub>"
sudo tailscale up --ssh
staragent node-ts --sudo
```

`staragent node-ts` checks that Tailscale is installed and already connected, then
starts the `staragent-node` tmux system session and configures `tailscale serve`.
If Tailscale is not ready, it prints the `tailscale up --ssh` command to run first.
Use `--sudo` when `tailscale serve` requires root privileges.

For LAN or another network layer that does not need `tailscale serve`, use:

```bash
staragent node
```

Before adding the Node, verify it from the Hub machine:

```bash
staragent verify-node <node-host-or-100.x-ip>
```

Add the reachable Host and Port in the Hub dashboard, for example `100.x.x.x` and `8081`.
If the Node uses a non-default port, enter that port explicitly, for example `8082`.

## Acknowledgements

StarAgent's CLI transcript parsing is adapted from ideas and code in [botmux](https://github.com/deepcoldy/botmux). The Launcher's local-browser, SSH, and `--no-open` startup behavior is inspired by [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness). The dashboard visual style is inspired by the [Tailscale admin console](https://tailscale.com/). Markdown preview follows [GitHub Flavored Markdown](https://github.github.com/gfm/) conventions. The web terminal uses [xterm.js](https://xtermjs.org/), and file preview highlighting uses [highlight.js](https://highlightjs.org/).

## License

MIT. See [LICENSE](LICENSE).
