# StarAgent 桌面版

[English](DESKTOP.md)

StarAgent Desktop 是现有 Launcher 和 Hub 的原生入口，不会再实现一套 Dashboard。桌面版启动
或连接的仍然是浏览器版所使用的同一套 HTTP 与 WebSocket 应用，持久终端后端按平台选择。

## 方案选择

桌面壳采用 [Tauri 2](https://v2.tauri.app/)。Tauri 复用操作系统 WebView，并且能为 Windows、
Linux 和 macOS 生成原生安装包；桌面层只负责进程生命周期、连接选择和系统集成，不在 Electron
中复制现有前端。

| 平台 | 桌面壳 | 本机 Launcher 运行时 | 安装包 |
| --- | --- | --- | --- |
| Windows x64 | 原生 Tauri/WebView2 | 内置 Python runtime + Windows ConPTY | NSIS `.exe` |
| Linux x64 | 原生 Tauri/WebKitGTK | 本机 StarAgent CLI + tmux | `.deb`、`.AppImage` |
| macOS Intel + Apple Silicon | Universal Tauri/WKWebView | 本机 StarAgent CLI + tmux | `.app`、`.dmg` |

Windows runtime 通过 PyInstaller 打包为 Tauri sidecar；`pywinpty` 管理原生 pseudoconsole，
StarAgent 为每个 Session 保留共享进程与有上限的 scrollback，因此关闭某个终端页面只会 detach。
退出整个桌面应用会停止本机 runtime 与其中的 Session。三个平台都可以在没有本机 runtime 前置
依赖的情况下连接已有 StarAgent Hub。

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

Windows 会把固定参数直接传给安装包内的 `staragent-runtime.exe`，不会调用 `wsl.exe`；WebView
也不能传入任意 runtime 命令。

## 本机依赖

Windows 本机模式只需要带 WebView2 的受支持 Windows 系统。安装包已包含 StarAgent、Python、
pywinpty 与 Dashboard 静态资源，不使用 WSL 或 tmux。Codex、Claude Code 等 Harness CLI 按需
安装，可直接在 Agents 页面一键完成。Codex 与 Claude Code 使用官方原生 PowerShell 安装器；
OpenCode 只下载官方 Windows 可执行文件，按 GitHub 发布的 SHA-256 校验后安装到用户的
`.opencode\bin` 目录。这些推荐路线不会安装 Node.js 或 npm。npmjs、npmmirror 与腾讯云仅作为
已有 npm 用户的显式备用项，registry 参数只对当次命令生效。

Launcher 启动后，**Current Node** 会显示内置 ConPTY、Tailscale 与 Node.js/npm 的实际状态。
当系统提供 `winget` 时，可以通过经过审核的 Windows Package Manager 入口安装缺少的可选工具；
Node.js 同时提供明确的 npmmirror 国内下载入口。

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

欢迎页会显示当前后端（`Windows ConPTY` 或 `tmux`），并在系统 runtime 缺失时给出可复制命令。
macOS/Linux 桌面应用通常拿不到 shell 启动文件中的 `PATH`，因此桌面端会通过登录 shell 主动
解析它。

## 自动更新

连接窗口的版本号旁提供持久化的 **Stable / Nightly** 更新通道选择。Stable 跟随 GitHub 最新正式
Release；Nightly 是由相关 `dev` commit 自动构建的滚动预发布，每次构建都有单调递增的版本号（例如
`0.1.3-dev.142`）并嵌入完整源码 commit。预发布安装默认选择 Nightly，正式版本默认选择 Stable；
切换通道后会立即检查，并在本机记住选择。

桌面应用每次启动也会检查所选通道。发现更新的 SemVer 后，连接窗口会显示版本、短 commit 和
Release Notes，不会打断用户；点击右上角版本号可以手动检查。安装始终需要用户确认：点击**更新并
重启**后，应用会展示下载进度、验证更新包签名、安装与当前包类型匹配的产物，然后重新启动。切换
通道不会强制降级；如果当前 Nightly 超前于 Stable，需要等 Stable 的 SemVer 追平后才会提示更新。

更新会停止由桌面应用持有的 Launcher runtime。确认更新前请保存本地 Session 中正在进行的工作；
Windows Session 尤其需要注意，因为它运行在内置 runtime 中。选择“稍后”不会停止任何进程。

Stable 与 Nightly 更新包安装前都必须通过同一把 Tauri updater 公钥验签。应用只接受编译时写死的
两个 manifest 地址，WebView 不能传入任意更新服务器。滚动 Nightly 会先上传带唯一名字的安装包，
最后替换包含完整源码 commit 的 manifest，避免发布切换期间出现安装包与签名不匹配。这和 Windows
Authenticode、Apple notarization 等操作系统发行者签名不是同一回事。Dashboard WebView 无权调用
更新命令，只有内置连接窗口具备桌面 capability。

早于此功能的旧安装包无法凭空获得 updater，需要手动安装首个支持自动更新的桌面版本；此后的版本
即可走应用内更新通道。已经带有旧版 Stable-only updater、但早于通道选择器的构建，也需要手动安装
一次支持通道的安装包（或等待下一个 Stable 桥接版本），之后才能在应用内选择 Nightly。

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

Windows 构建还需要 Python 3.11 与 PyInstaller：先生成内置 runtime，再交给 Tauri 打包。GitHub
workflow 已自动执行这些步骤，并把带 target triple 后缀的 sidecar 放入
`desktop/src-tauri/binaries/`。PyInstaller 必须在 Windows 上运行，不能从 Linux/macOS 交叉
生成 Windows 可执行文件。本机 Windows 构建命令如下：

```powershell
python -m pip install . pyinstaller
cd desktop
npm run runtime:build:windows
npm run desktop:build
```

产物位于 `desktop/src-tauri/target/*/release/bundle/`。安装包应在对应操作系统上构建；例如
NSIS/MSI 和 DMG 都依赖各自平台的工具链。

仓库中的 `Desktop` GitHub Actions 工作流会构建 Linux x64、Windows x64 和同时支持 Intel/
Apple Silicon 的 macOS Universal 版本。桌面代码 PR、`dev`/`main` push、带版本号的 GitHub Release
发布或手动触发时，安装包会分别作为 workflow artifact 上传。正式 Release 会签名并发布 Stable
资产与 manifest；相关 `dev` push 会为 Python、npm、Cargo 和 Tauri 计算同一个 Nightly 版本，签名
updater 产物，再原子更新滚动的 `nightly` 预发布。manifest 包含包类型维度，因此 `.deb`、AppImage、
NSIS、MSI 和 macOS 客户端不会收到不兼容的包。单次 workflow Artifact 仍需登录 GitHub 后下载。

Release 构建依赖仓库 Secret `TAURI_SIGNING_PRIVATE_KEY`。对应公钥已写入 `tauri.conf.json`；私钥绝不
进入仓库，且必须安全备份。丢失私钥后，已有安装将无法升级到用新密钥签名的版本。CI 还会检查
Release tag 是否与 Python、npm、Cargo 和 Tauri 中的版本号完全一致。

## 正式发布前

Updater 签名已经保护应用内升级通道，但开发构建仍未签名，操作系统也可能提示“未知发行者”。正式
发行还应增加 Apple Developer ID 签名/notarization，以及 Windows 主程序、sidecar 与 NSIS 的
Authenticode 签名。

Windows sidecar 与 ConPTY 后端会经过 Windows CI 冒烟测试。Linux/macOS 暂不内置 runtime，
因为它们仍可与源码安装共用已经稳定的 tmux 后端。
