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
| Linux x64 | Native Tauri/WebKitGTK | Native StarAgent CLI + tmux | `.deb`, `.AppImage` |
| macOS Intel + Apple Silicon | Universal Tauri/WKWebView | Native StarAgent CLI + tmux | `.app`, `.dmg` |

The Windows runtime is packaged as a Tauri sidecar with PyInstaller. `pywinpty` owns the native
pseudoconsole, while StarAgent keeps one shared process and bounded scrollback buffer per Session so
closing a terminal view only detaches that view. Closing the whole desktop application stops its local
runtime and Sessions. All three apps can connect to an existing StarAgent Hub without local runtime
prerequisites.

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

On Windows the fixed Launcher arguments are passed directly to the bundled `staragent-runtime.exe`.
There is no `wsl.exe` invocation. The app never accepts a runtime command from the WebView.

## Local prerequisites

Windows local mode requires a supported Windows installation with WebView2. StarAgent, Python,
pywinpty, and the Dashboard assets are included in the installer; WSL and tmux are not used. Coding
Harness CLIs are intentionally separate and can be installed from the Agents page using an official
npm registry or one of the China-friendly mirrors.

Linux and macOS local mode require StarAgent and tmux before opening the desktop app.

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

The welcome screen reports the selected backend (`Windows ConPTY` or `tmux`) and shows a copyable
command when a system runtime is missing. On macOS and Linux it resolves the user's login-shell
`PATH`, because GUI applications do not normally inherit shell startup files.

## Automatic updates

The desktop app checks the latest GitHub Release once at startup. When a newer SemVer release is
available, the connection window shows its version and release notes without interrupting the user.
The version chip can also run a manual check. Installation always requires confirmation: after the
user selects **Update & restart**, the app reports download progress, verifies the package signature,
installs the bundle matching the current package type, and relaunches.

Updating stops the Launcher runtime owned by the desktop app. Save work in a running local Session
before confirming an update—especially on Windows, where those Sessions live inside the bundled
runtime. Merely dismissing or postponing the update does not stop anything.

Release packages are verified with Tauri's updater signature before installation. This signature is
separate from operating-system publisher trust such as Windows Authenticode and Apple notarization.
The Dashboard WebView cannot invoke update commands; only the bundled connection window has the
desktop capability.

Existing installs that predate the updater must install the first updater-enabled desktop release
manually. Updates after that release use the in-app channel.

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

The Windows build also needs Python 3.11 and PyInstaller to create the bundled runtime before Tauri is
invoked. The GitHub workflow performs these steps and places the target-suffixed sidecar in
`desktop/src-tauri/binaries/`. PyInstaller must run on Windows; it does not cross-compile Windows
executables from Linux or macOS. A local Windows build is:

```powershell
python -m pip install . pyinstaller
cd desktop
npm run runtime:build:windows
npm run desktop:build
```

Installers are written below `desktop/src-tauri/target/*/release/bundle/`. Installer creation should run
on the target operating system; in particular, MSI/NSIS and DMG tooling are platform-specific.

The `Desktop` GitHub Actions workflow builds Linux x64, Windows x64, and a universal macOS binary. It
runs for desktop pull requests, `dev`/`main` pushes, published GitHub Releases, or manual dispatch,
and uploads each installer set as a workflow artifact. For a published Release, it signs each updater
payload, attaches installers and signatures to that Release's **Assets**, and generates `latest.json`
for the in-app channel. The manifest has bundle-specific entries, so `.deb`, AppImage, NSIS, MSI, and
macOS clients do not receive an incompatible package. Development artifacts remain available from the
individual workflow run and require a signed-in GitHub account to download.

Release builds require the repository secret `TAURI_SIGNING_PRIVATE_KEY`. The corresponding public key
is committed in `tauri.conf.json`; the private key must never be committed and must be backed up
securely. Losing it prevents future releases from updating existing installations. CI also rejects a
Release whose tag does not match the Python, npm, Cargo, and Tauri package versions.

## Release hardening

Updater signing protects the in-app update channel, but development installers remain unsigned and
the operating systems may still show an unknown-publisher warning. A public production channel should
also add Apple Developer ID signing/notarization and Authenticode signing for the Windows executable,
sidecar, and NSIS installer.

The Windows sidecar and ConPTY backend are covered by Windows CI smoke tests. Linux/macOS runtime
bundling remains separate future work because those packages already share the established tmux
backend with source installations.
