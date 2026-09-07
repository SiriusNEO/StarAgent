# StarAgent Desktop

[简体中文](DESKTOP.zh-CN.md)

StarAgent Desktop is a native gateway around the existing Launcher and Hub. It does not duplicate the
Dashboard: the desktop app starts or connects to the same HTTP and WebSocket application used by the
browser UI, with a platform-specific persistent terminal backend.

## Decision

The desktop shell uses [Tauri 2](https://v2.tauri.app/). Tauri reuses the operating system WebView and
supports native bundles for Windows, Linux, and macOS. This keeps the shell focused on process
lifecycle, connection selection, and OS integration instead of duplicating the frontend in Electron.

| Platform | Desktop shell | Local Launcher runtime | Bundles |
| --- | --- | --- | --- |
| Windows x64 | Native Tauri/WebView2 | Bundled Python runtime + Windows ConPTY | NSIS `.exe` |
| Linux x64 | Native Tauri/WebKitGTK | Bundled Python runtime + native PTY | `.deb`, `.AppImage` |
| macOS Apple Silicon / Intel | Native Tauri/WKWebView | Bundled Python runtime + native PTY | architecture-specific `.app`, `.dmg` |

The StarAgent runtime and Dashboard assets are packaged on every platform as a PyInstaller Tauri
sidecar. Windows uses `pywinpty`/ConPTY; macOS and Linux use the operating system PTY API. StarAgent
keeps one shared process and bounded scrollback buffer per Session, so closing a terminal view only
detaches that view. Closing the whole desktop application gracefully stops its local runtime and
Sessions. Every desktop package can also connect to an existing StarAgent Hub without starting the
local sidecar.

## Security boundary

The bundled `main` window is the only window with a Tauri capability. It can inspect prerequisites,
start the fixed local Launcher command, and open a validated HTTP(S) endpoint.

The Dashboard is loaded into a separately labelled WebView with no Tauri capability and no configured
remote IPC origin. Top-level navigation is restricted to the selected Hub origin. Links requesting a
new window are handed to the default browser only for `http`, `https`, and `mailto` URLs. This keeps a
Hub page—even a compromised one—outside the desktop command boundary.

The local runtime binds to `127.0.0.1:8765`. The app verifies the public runtime identity, reuses an
already-running bundled instance, or starts the packaged sidecar with fixed arguments:

```text
staragent-runtime --host 127.0.0.1 --port 8765 --mode launcher
```

There is no `wsl.exe`, system Python, or system `staragent` invocation. The app never accepts a
runtime command from the WebView. It forwards the user's login-shell `PATH` to the sidecar so
already-installed Harness CLIs remain discoverable when the app was opened from Finder or another
graphical launcher.

## Local prerequisites

Local mode has no StarAgent, Python, tmux, npm, or WSL prerequisite. The installer includes the
StarAgent runtime, Dashboard assets, and terminal backend. Windows additionally requires a supported
installation with WebView2; macOS uses WKWebView and Linux packages install their WebKitGTK package
dependency through the native package manager.

Coding Harness CLIs are intentionally separate applications and can be installed from the Agents
page. Codex and Claude Code use official native installers where available. OpenCode's Windows route
downloads the official executable, verifies the SHA-256 digest published by GitHub, and installs it
in the user's `.opencode\bin` directory. npmjs, npmmirror, and Tencent Cloud remain clearly labelled
fallbacks for systems that already provide npm; their registry argument applies only to that command.

Once the Launcher is running, **Current Node** reports the bundled terminal backend plus optional
Tailscale and Node.js/npm integrations. Missing optional tools keep their reviewed platform-native
installation paths and China-friendly resources. They are not required to open StarAgent itself.
The welcome screen reports `Windows ConPTY` or `Native PTY`; a packaged build never asks the user to
install a separate StarAgent runtime.

## Automatic updates

The connection window has a persistent **Stable / Nightly** update selector beside the version chip.
Stable follows the latest normal GitHub Release. Nightly follows a rolling prerelease built from each
relevant `dev` commit; every build receives a monotonic version such as `0.1.3-dev.142` and embeds
the full source commit. Prerelease installations default to Nightly, while normal releases default to
Stable. Selecting another channel immediately checks it and remembers the choice on this machine.

The app also checks the selected channel once at startup. When a newer SemVer build is available, the
connection window shows its version, short commit, and release notes without interrupting the user.
The version chip can run a manual check. Installation always requires confirmation: after the user
selects **Update & restart**, the app reports download progress, verifies the package signature,
installs the bundle matching the current package type, and relaunches. Switching channels never forces
a downgrade; after moving from an ahead-of-Stable Nightly, Stable becomes available when its SemVer
catches up.

Updating stops the Launcher runtime owned by the desktop app. Save work in a running local Session
before confirming an update, because desktop Sessions live inside that bundled runtime on every
platform. Merely dismissing or postponing the update does not stop anything.

Stable and Nightly packages are verified with the same Tauri updater public key before installation.
The app accepts only two compiled-in manifest endpoints, so the WebView cannot supply an arbitrary
update server. Each rolling Nightly manifest is uploaded after its uniquely named packages and carries
the full source commit; this prevents a package/signature mismatch while the release is replaced.
Tauri's signature is separate from operating-system publisher trust such as Windows Authenticode and
Apple notarization. The Dashboard WebView cannot invoke update commands; only the bundled connection
window has the desktop capability.

Existing installs that predate the updater must install the first updater-enabled desktop release
manually. Builds that have the original Stable-only updater but predate the channel selector likewise
need one manual channel-aware installer (or the next Stable bridge release) before they can opt into
Nightly. Updates after that use the selected in-app channel.

## Develop and build

Install the [Tauri prerequisites](https://v2.tauri.app/start/prerequisites/) plus Python 3.11 and
PyInstaller. From the repository root:

```bash
python -m pip install . pyinstaller
cd desktop
npm ci
npm run desktop:dev
```

Use `npm run desktop:build` instead of `desktop:dev` to create a native installer. Both commands first
freeze the bundled runtime and then invoke Tauri. The GitHub workflow performs the same steps and
places the target-suffixed sidecar in `desktop/src-tauri/binaries/`. PyInstaller must run natively on
the target operating system and CPU architecture.

```bash
npm run desktop:build
```

Installers are written below `desktop/src-tauri/target/*/release/bundle/`. Installer creation should run
on the target operating system; in particular, MSI/NSIS and DMG tooling are platform-specific.

The `Desktop` GitHub Actions workflow builds Linux x64, Windows x64, macOS Apple Silicon, and macOS
Intel packages. The native macOS packages replace the former universal shell because a frozen Python
sidecar must be produced and tested for the same CPU architecture. It runs for desktop pull requests,
`dev`/`main` pushes, published versioned GitHub Releases, or manual
dispatch, and uploads each installer set as a workflow artifact. A versioned Release signs and
publishes the Stable assets and manifest. A relevant `dev` push derives one synchronized Nightly
version for Python, npm, Cargo, and Tauri, signs the updater payloads, and atomically refreshes the
rolling `nightly` prerelease. The manifest has bundle-specific entries, so `.deb`, AppImage, NSIS,
MSI, and macOS clients do not receive an incompatible package. Workflow artifacts remain available
from the individual run and require a signed-in GitHub account to download.

Release builds require the repository secret `TAURI_SIGNING_PRIVATE_KEY`. The corresponding public key
is committed in `tauri.conf.json`; the private key must never be committed and must be backed up
securely. Losing it prevents future releases from updating existing installations. CI also rejects a
Release whose tag does not match the Python, npm, Cargo, and Tauri package versions.

## Release hardening

Updater signing protects the in-app update channel, but development installers remain unsigned and
the operating systems may still show an unknown-publisher warning. A public production channel should
also add Apple Developer ID signing/notarization and Authenticode signing for the Windows executable,
sidecar, and NSIS installer.

Every sidecar is started in CI. The smoke test verifies its bundled runtime identity and creates a
real Session through ConPTY on Windows or the native PTY backend on Linux/macOS before an installer is
published.
