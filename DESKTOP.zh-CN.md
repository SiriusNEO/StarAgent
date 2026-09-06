# StarAgent 桌面版

[English](DESKTOP.md)

StarAgent Desktop 是现有 Launcher 和 Hub 的原生入口，不会再实现一套 Dashboard。桌面版启动
或连接的仍然是浏览器版所使用的同一套 HTTP、WebSocket、tmux 与 PTY 运行时。

## 方案选择

桌面壳采用 [Tauri 2](https://v2.tauri.app/)。Tauri 复用操作系统 WebView，并且能为 Windows、
Linux 和 macOS 生成原生安装包；桌面层只负责进程生命周期、连接选择和系统集成，不在 Electron
中复制现有前端。

现有 Python 运行时依赖 `tmux`、`pty`、`termios`、`fcntl` 和 `os.setsid`，因此 Windows 安装包
可以是原生应用，但在没有重写终端后端之前，本地 Session 运行时不能冒充原生 Windows 实现。
第一版保留经过验证的运行时：

| 平台 | 桌面壳 | 本机 Launcher 运行时 | 安装包 |
| --- | --- | --- | --- |
| Windows x64 | 原生 Tauri/WebView2 | 默认 WSL2 发行版 | NSIS `.exe` |
| Linux x64 | 原生 Tauri/WebKitGTK | 本机 StarAgent CLI + tmux | `.deb`、`.AppImage` |
| macOS Intel + Apple Silicon | Universal Tauri/WKWebView | 本机 StarAgent CLI + tmux | `.app`、`.dmg` |

即使不安装本机运行时，三个平台的桌面版都可以直接连接已有 StarAgent Hub。

## 安全边界

只有内置的 `main` 连接窗口拥有 Tauri capability，可以检测依赖、启动固定的本机 Launcher
命令，以及打开通过校验的 HTTP(S) 地址。

Dashboard 会加载到另一个没有 Tauri capability、也没有 remote IPC 权限的 WebView。顶层导航
只能留在用户选择的 Hub 同源地址；新窗口链接只会把 `http`、`https` 与 `mailto` 交给系统浏览器。
因此，即使 Hub 页面被攻击，也无法越过边界调用桌面命令。

本机运行时只监听 `127.0.0.1:8765`。桌面版会先确认这个端口返回的确实是 StarAgent 页面，
存在时直接复用，否则启动：

```text
staragent dashboard --host 127.0.0.1 --port 8765 --mode launcher
```

Windows 通过 `wsl.exe --exec sh -lc ...` 执行同一条固定命令；WebView 不能传入任意 shell 命令。

## 本机依赖

Linux（以 Debian/Ubuntu 为例）：

```bash
sudo apt install tmux pipx
pipx install git+https://github.com/SiriusNEO/StarAgent.git
pipx ensurepath
```

macOS：

```bash
brew install tmux pipx
pipx install git+https://github.com/SiriusNEO/StarAgent.git
pipx ensurepath
```

Windows 先准备 WSL2：

```powershell
wsl --install -d Ubuntu
wsl -- bash -lc "sudo apt update && sudo apt install -y tmux pipx && pipx install git+https://github.com/SiriusNEO/StarAgent.git && pipx ensurepath"
```

欢迎页会自动完成同样的检测，并在缺少依赖时给出可复制命令。macOS/Linux 桌面应用通常拿不到
shell 启动文件中的 `PATH`，因此桌面端会通过登录 shell 主动解析它。

## 开发与构建

先安装当前平台的 [Tauri 构建依赖](https://v2.tauri.app/start/prerequisites/)，然后运行：

```bash
cd desktop
npm ci
npm run desktop:dev
```

为当前系统构建原生安装包：

```bash
cd desktop
npm ci
npm run desktop:build
```

产物位于 `desktop/src-tauri/target/*/release/bundle/`。安装包应在对应操作系统上构建；例如
NSIS/MSI 和 DMG 都依赖各自平台的工具链。

仓库中的 `Desktop` GitHub Actions 工作流会构建 Linux x64、Windows x64 和同时支持 Intel/
Apple Silicon 的 macOS Universal 版本。桌面代码 PR、`dev`/`main` push、GitHub Release 发布或
手动触发时，安装包会分别作为 workflow artifact 上传；如果由已发布 Release 触发，后续 job
还会把这些安装包附加到该 Release 的 **Assets**。开发版 Artifact 需要登录 GitHub 后从对应的
workflow run 下载。

## 正式发布前

目前 CI 产出的是开发用未认证安装包（macOS 使用 ad-hoc 签名）。进入官方发布通道前还应补充：

1. Apple Developer ID 签名与 notarization。
2. Windows 可执行文件与 NSIS 安装包的 Authenticode 签名。
3. Tauri updater 签名和独立的桌面版 release manifest。
4. 同步 `staragent/__init__.py`、`desktop/package.json`、`desktop/src-tauri/Cargo.toml` 与
   `desktop/src-tauri/tauri.conf.json` 的版本号。

暂不把 Python runtime 硬塞进安装包：PyInstaller sidecar 可以让 Linux/macOS 少安装 Python，
但 tmux 仍然是系统依赖，也不能让 Windows Session 突然变成原生实现。下一步更合理的是引导式
WSL 安装；如果确实需要纯原生 Windows，再把 ConPTY process supervisor 作为独立后端项目实现。
