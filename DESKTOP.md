# StarAgent Desktop

[简体中文](DESKTOP.zh-CN.md)

StarAgent Desktop is a small native gateway around the existing Launcher and Hub. It is intentionally
not a second implementation of the Dashboard: the desktop app starts or connects to the same HTTP,
WebSocket, tmux, and PTY stack used by the browser UI.

## Decision

The desktop shell uses [Tauri 2](https://v2.tauri.app/). Tauri reuses the operating system WebView and
supports native bundles for Windows, Linux, and macOS. This keeps the shell focused on process
lifecycle, connection selection, and OS integration instead of duplicating the frontend in Electron.

The existing Python runtime is POSIX-specific (`tmux`, `pty`, `termios`, `fcntl`, and `os.setsid`). A
Windows installer can therefore be native, but the current local session runtime cannot honestly be
called native Windows without replacing tmux/PTY with a separate ConPTY process supervisor. The first
desktop architecture preserves the tested runtime instead:

| Platform | Desktop shell | Local Launcher runtime | Bundles |
| --- | --- | --- | --- |
| Windows x64 | Native Tauri/WebView2 | Default WSL2 distribution | NSIS `.exe` |
| Linux x64 | Native Tauri/WebKitGTK | Native StarAgent CLI + tmux | `.deb`, `.AppImage` |
| macOS Intel + Apple Silicon | Universal Tauri/WKWebView | Native StarAgent CLI + tmux | `.app`, `.dmg` |

All three apps can connect to an existing StarAgent Hub without installing the local runtime.

## Security boundary

The bundled `main` window is the only window with a Tauri capability. It can inspect prerequisites,
start the fixed local Launcher command, and open a validated HTTP(S) endpoint.

The Dashboard is loaded into a separately labelled WebView with no Tauri capability and no configured
remote IPC origin. Top-level navigation is restricted to the selected Hub origin. Links requesting a
new window are handed to the default browser only for `http`, `https`, and `mailto` URLs. This keeps a
Hub page—even a compromised one—outside the desktop command boundary.

The local runtime binds to `127.0.0.1:8765`. The app first verifies that the response is actually a
StarAgent login page, reuses an already-running instance, or starts:

```text
staragent dashboard --host 127.0.0.1 --port 8765 --mode launcher
```

On Windows the same fixed command runs through `wsl.exe --exec sh -lc ...`. The app never accepts a
shell command from the WebView.

## Local prerequisites

For local mode, install StarAgent and tmux before opening the desktop app.

Linux (Debian/Ubuntu example):

```bash
sudo apt install tmux pipx
pipx install git+https://github.com/SiriusNEO/StarAgent.git
pipx ensurepath
```

macOS:

```bash
brew install tmux pipx
pipx install git+https://github.com/SiriusNEO/StarAgent.git
pipx ensurepath
```

Windows first needs WSL2 and a distribution:

```powershell
wsl --install -d Ubuntu
wsl -- bash -lc "sudo apt update && sudo apt install -y tmux pipx && pipx install git+https://github.com/SiriusNEO/StarAgent.git && pipx ensurepath"
```

The welcome screen performs the same checks and shows a copyable command when something is missing.
On macOS and Linux it resolves the user's login-shell `PATH`, because GUI applications do not normally
inherit shell startup files.

## Develop and build

Install the [Tauri prerequisites](https://v2.tauri.app/start/prerequisites/) for the host platform, then:

```bash
cd desktop
npm ci
npm run desktop:dev
```

Build a native installer on the current platform:

```bash
cd desktop
npm ci
npm run desktop:build
```

Installers are written below `desktop/src-tauri/target/*/release/bundle/`. Installer creation should run
on the target operating system; in particular, MSI/NSIS and DMG tooling are platform-specific.

The `Desktop` GitHub Actions workflow builds Linux x64, Windows x64, and a universal macOS binary. It
runs for desktop pull requests, `dev`/`main` pushes, published GitHub Releases, or manual dispatch,
and uploads each installer set as a workflow artifact. For a published Release, a follow-up job also
attaches the generated installers to that Release's **Assets** section. Development artifacts remain
available from the individual workflow run and require a signed-in GitHub account to download.

## Release hardening

CI currently creates unsigned development installers (the macOS build uses an ad-hoc signature). A
public production channel should add:

1. Apple Developer ID signing and notarization.
2. Authenticode signing for the Windows executable and NSIS installer.
3. Tauri updater signatures and a separate desktop release manifest.
4. Version synchronization between `staragent/__init__.py`, `desktop/package.json`,
   `desktop/src-tauri/Cargo.toml`, and `desktop/src-tauri/tauri.conf.json`.

Bundling the Python runtime is deliberately deferred: a PyInstaller sidecar can make Linux/macOS more
self-contained, but tmux remains a system dependency and that sidecar still cannot become a native
Windows session runtime. Guided WSL provisioning is the smaller, more maintainable next step. A native
Windows runtime should only be undertaken as an explicit ConPTY backend project, not hidden inside the
packaging layer.
