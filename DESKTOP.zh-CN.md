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
| Linux x64 | 原生 Tauri/WebKitGTK | 内置 Python runtime + 原生 PTY | `.deb`、`.AppImage` |
| macOS Apple Silicon / Intel | 原生 Tauri/WKWebView | 内置 Python runtime + 原生 PTY | 分架构 `.app`、`.dmg` |

三个平台都会通过 PyInstaller 将 StarAgent runtime 与 Dashboard 资源打进 Tauri sidecar。
Windows 使用 `pywinpty`/ConPTY，macOS 与 Linux 使用系统原生 PTY。StarAgent 为每个 Session
保留共享进程与有上限的 scrollback，因此关闭某个终端页面只会 detach；退出整个桌面应用会先
清理本机 Session，再停止 runtime。三个平台也都可以不启动本机 sidecar，直接连接已有 Hub。

## 安全边界

只有内置的 `main` 连接窗口拥有 Tauri capability，可以检测依赖、启动固定的本机 Launcher
命令，以及打开通过校验的 HTTP(S) 地址。

Dashboard 会加载到另一个没有 Tauri capability、也没有 remote IPC 权限的 WebView。顶层导航
只能留在用户选择的 Hub 同源地址；新窗口链接只会把 `http`、`https` 与 `mailto` 交给系统浏览器。
因此，即使 Hub 页面被攻击，也无法越过边界调用桌面命令。

本机运行时只监听 `127.0.0.1:8765`。桌面版会校验公开的 runtime identity，存在内置实例时
直接复用，否则用固定参数启动安装包中的 sidecar：

```text
staragent-runtime --host 127.0.0.1 --port 8765 --mode launcher
```

桌面版不会调用 `wsl.exe`、系统 Python 或系统 `staragent`，WebView 也不能传入任意 runtime
命令。应用会把登录 shell 的 `PATH` 交给 sidecar，因此从 Finder 等图形界面启动时，仍能发现
用户已经安装的 Harness CLI。

## 本机依赖

本机模式不要求预装 StarAgent、Python、tmux、npm 或 WSL。安装包已经包含 StarAgent runtime、
Dashboard 资源和终端后端。Windows 还需要系统支持 WebView2；macOS 使用 WKWebView，Linux 的
原生软件包会通过包管理器声明 WebKitGTK 依赖。

Codex、Claude Code 等 Harness CLI 是按需安装的独立应用，可以直接从 Agents 页面安装。可用时
优先提供官方原生安装方式；OpenCode 的 Windows 入口会下载官方可执行文件、校验 GitHub 发布的
SHA-256，再安装到 `.opencode\bin`。npmjs、npmmirror 与腾讯云是系统已有 npm 时的备用项，
registry 参数只影响当次安装命令。

Launcher 启动后，**Current Node** 会显示内置终端后端，以及可选的 Tailscale、Node.js/npm
集成。缺少的可选工具仍提供经过审核的系统原生安装入口和国内资源，但它们不影响 StarAgent 自身
启动。欢迎页会显示 `Windows ConPTY` 或 `Native PTY`；正式桌面安装包不会再要求另装
StarAgent runtime。

## 自动更新

连接窗口的版本号旁提供持久化的 **Stable / Nightly** 更新通道选择。Stable 跟随 GitHub 最新正式
Release；Nightly 是由相关 `dev` commit 自动构建的滚动预发布，每次构建都有单调递增的版本号（例如
`0.1.3-dev.142`）并嵌入完整源码 commit。预发布安装默认选择 Nightly，正式版本默认选择 Stable；
切换通道后会立即检查，并在本机记住选择。

桌面应用每次启动也会检查所选通道。发现更新的 SemVer 后，连接窗口会显示版本、短 commit 和
Release Notes，不会打断用户；点击右上角版本号可以手动检查。安装始终需要用户确认：点击**更新并
重启**后，应用会展示下载进度、验证更新包签名、安装与当前包类型匹配的产物，然后重新启动。切换
通道不会强制降级；如果当前 Nightly 超前于 Stable，需要等 Stable 的 SemVer 追平后才会提示更新。

更新会停止由桌面应用持有的 Launcher runtime。确认更新前请保存本地 Session 中正在进行的工作，
因为三个平台的桌面 Session 都运行在内置 runtime 中。选择“稍后”不会停止任何进程。

Stable 与 Nightly 更新包安装前都必须通过同一把 Tauri updater 公钥验签。应用只接受编译时写死的
两个 manifest 地址，WebView 不能传入任意更新服务器。滚动 Nightly 会先上传带唯一名字的安装包，
最后替换包含完整源码 commit 的 manifest，避免发布切换期间出现安装包与签名不匹配。这和 Windows
Authenticode、Apple notarization 等操作系统发行者签名不是同一回事。Dashboard WebView 无权调用
更新命令，只有内置连接窗口具备桌面 capability。

早于此功能的旧安装包无法凭空获得 updater，需要手动安装首个支持自动更新的桌面版本；此后的版本
即可走应用内更新通道。已经带有旧版 Stable-only updater、但早于通道选择器的构建，也需要手动安装
一次支持通道的安装包（或等待下一个 Stable 桥接版本），之后才能在应用内选择 Nightly。

## 开发与构建

先安装当前平台的 [Tauri 构建依赖](https://v2.tauri.app/start/prerequisites/)、Python 3.11 与
PyInstaller，再从仓库根目录运行：

```bash
python -m pip install . pyinstaller
cd desktop
npm ci
npm run desktop:dev
```

将最后一条命令换成 `npm run desktop:build` 即可构建原生安装包。两个命令都会先冻结内置
runtime，再调用 Tauri。GitHub workflow 会执行相同步骤，并把带 target triple 后缀的 sidecar
放入 `desktop/src-tauri/binaries/`。PyInstaller 必须在目标操作系统和 CPU 架构上原生运行。

```bash
npm run desktop:build
```

产物位于 `desktop/src-tauri/target/*/release/bundle/`。安装包应在对应操作系统上构建；例如
NSIS/MSI 和 DMG 都依赖各自平台的工具链。

仓库中的 `Desktop` GitHub Actions 工作流会分别构建 Linux x64、Windows x64、macOS Apple
Silicon 和 macOS Intel 版本。冻结后的 Python sidecar 必须与 CPU 架构一致，因此 macOS 从原来的
Universal 壳改为两份经过原生测试的安装包。桌面代码 PR、`dev`/`main` push、带版本号的 GitHub Release
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

CI 会实际启动每个平台的 sidecar，校验内置 runtime identity，并在发布安装包前通过 Windows
ConPTY 或 Linux/macOS 原生 PTY 创建一个真实 Session。
