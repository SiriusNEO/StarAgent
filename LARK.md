# StarAgent Lark Integration

StarAgent 的 Lark 接入是一个轻量 command adapter。Lark 只作为通知和遥控入口，StarAgent Hub / Node / tmux session 仍然是唯一的 session source of truth。

当前交互模型：

- 和 StarAgent Bot 私聊只用于管理 sessions，不会把普通消息转发给 agent。
- 每个 agent session 推荐对应一个飞书对话群，并把这个群绑定到对应 StarAgent session。
- 绑定后，同一个群里的普通消息才会被发送到 agent session。
- StarAgent 不会把 CLI raw output 持续推到飞书里；群里只自动回最终答复。
- Dashboard 的 Lark 页面负责配置、教程、worker 状态和启动/停止入口。

## 能力范围

私聊里用来管理 sessions：

```text
/help
/sessions
/status <node/session>
/history <node/session> [count]
/tail <node/session> [lines]
/open <node/session>
```

对话群里先绑定一次 session，后续命令默认使用当前绑定：

```text
/use <node/session>
/use
/status
/history [count]
/tail [lines]
/open
/unbind
```

私聊机器人时可以直接管理 sessions：

```text
/help
/sessions
/status local/my-session
/history local/my-session 20
/tail local/my-session 120
/open local/my-session
```

和 agent 对话时，给该 session 准备一个飞书对话群，在群设置里添加 StarAgent Bot，然后在群里写：

```text
@StarAgent /use local/my-session
```

绑定成功后，在同一个群里继续发普通消息即可。如果飞书事件权限只投递 @ 机器人消息，则每条消息仍然需要 `@StarAgent`。

普通群消息会静默发送到 session；agent 完成后，群里只会收到一条最终答复。中间的 stdout/stderr、TUI redraw、测试日志不会自动刷屏到飞书里。

如果飞书里看不到之前的上下文，可以主动拉取结构化对话记录：

```text
@StarAgent /history 20
```

私聊里需要写完整 session：

```text
/history local/my-session 20
```

`/history` 默认显示最近 12 条，最多 30 条。它优先读取 Codex / Claude 等 CLI 的结构化 transcript；如果当前 session 没有结构化消息，会提示你改用 `/tail <node/session> 120` 查看原始终端输出。

也支持不带斜杠的管理前缀形式：

```text
staragent sessions
```

## 安装

```bash
pip install -e '.[lark]'
```

该 optional extra 会安装 Lark 官方 Python SDK `lark-oapi`。

## Lark 应用配置

在 Lark 开放平台创建一个企业自建应用：

1. 创建应用，记录 `App ID` 和 `App Secret`。
2. 启用机器人能力。
3. 权限管理中添加消息相关权限。
4. 事件订阅选择长连接 / WebSocket 模式。
5. 订阅事件 `im.message.receive_v1`。
6. 发布应用版本。
7. 私聊 StarAgent Bot 用于 session 管理。
8. agent 对话需要创建或使用一个飞书对话群，并从群里的 `设置` / `群机器人` / `添加机器人` 入口添加 StarAgent Bot。不要从“添加成员/邀请人”里找 Bot。

建议的最小权限：

```text
im:message
im:message:send_as_bot
im:message.group_at_msg:readonly
im:message.p2p_msg:readonly
```

说明：

- `im.message.receive_v1` 是接收用户消息的事件。
- 在权限管理里同时勾选 `Send and delete message reaction`，用于在 agent working 时给用户消息加大拇指，并在最终答复发出后移除。
- 私聊用 `/command` 管理 sessions。
- 群聊里通常用 `@机器人 /command` 触发。
- agent 输入只在已绑定的飞书对话群中生效。
- 当前 StarAgent 不需要文件、图片、卡片、群管理权限。

## 环境变量

必填：

```bash
export STARAGENT_LARK_APP_ID='cli_xxx'
export STARAGENT_LARK_APP_SECRET='xxx'
```

访问控制至少配置一种：

```bash
# 私聊推荐：限制到可信用户，支持 open_id / user_id / union_id
export STARAGENT_LARK_ALLOWED_USERS='ou_xxx,on_xxx'

# 群聊可选：限制到可信群
export STARAGENT_LARK_ALLOWED_CHATS='oc_xxx,oc_yyy'

# 仅测试用：放开所有 Lark 发送者
export STARAGENT_LARK_ALLOW_ALL=1
```

可选：

```bash
# /open 命令返回的 Dashboard 链接前缀
export STARAGENT_DASHBOARD_URL='https://staragent.example.com'

# 如果 Lark 应用配置了 verification token / encrypt key
export STARAGENT_LARK_VERIFICATION_TOKEN='xxx'
export STARAGENT_LARK_ENCRYPT_KEY='xxx'

# 如果需要操作远端 node，worker 环境里也要有 node token
export STARAGENT_NODE_TOKEN='<same token as nodes>'
# 或复用
export STARAGENT_AUTH_TOKEN='<hub token>'
```

## 启动

前台运行：

```bash
staragent lark
```

tmux 常驻运行：

```bash
tmux new -ds staragent-lark 'staragent lark'
```

Dashboard 里也可以打开 `Lark` 页面查看缺失配置、复制启动命令、启动/停止 `staragent-lark` worker，并查看最近的 tmux 输出。
保存 App ID / App Secret 后，可以点 `Test Connection` 验证凭证能否换取 tenant access token，并读取 Bot 信息。
如果测试超时，通常是运行 Hub 的机器没有直连外网，需要让 `staragent hub` 和 `staragent lark` 继承 `http_proxy` / `https_proxy`，同时保留 `NO_PROXY` 避免本机 Hub/Node 请求走代理。

也可以直接通过参数传入配置：

```bash
staragent lark \
  --app-id cli_xxx \
  --app-secret xxx \
  --allowed-chats oc_xxx \
  --dashboard-url https://staragent.example.com
```

## 使用示例

私聊 StarAgent Bot，列出所有 session：

```text
/sessions
```

查看状态：

```text
/status local/my-session
```

查看最近结构化对话：

```text
/history local/my-session 20
```

查看终端尾部输出：

```text
/tail local/my-session 120
```

打开 Dashboard session 页面：

```text
/open worker-1/codex-login-fix
```

在飞书对话群里绑定 agent session：

```text
@StarAgent /use worker-1/codex-login-fix
```

查看当前群绑定：

```text
@StarAgent /use
```

清除当前群绑定：

```text
@StarAgent /unbind
```

绑定后，在同一个群里继续发消息即可：

```text
继续跑测试并修掉失败项
```

如果飞书只把 @ 机器人消息投递给应用，则写成：

```text
@StarAgent 继续跑测试并修掉失败项
```

需要看原始终端输出时，显式拉取：

```text
@StarAgent /tail 120
```

需要看最近结构化对话时，显式拉取：

```text
@StarAgent /history 20
```

或打开 Dashboard terminal：

```text
@StarAgent /open
```

## 当前边界

- 不自动创建 StarAgent session。
- 不自动创建飞书群；需要你在飞书里手动创建或选择一个对话群。
- 只做飞书对话群到 StarAgent agent session 的持久绑定；一个群同时只能绑定一个 session。
- 不做 Lark 卡片、文件、截图、审批流。
- 私聊只做 session 管理，普通私聊文本不会转发给 agent。
- `system` session 只读，群聊只能绑定 `agent` session。
- 群聊自动回复只发送最终 agent reply；raw terminal output 只通过 `/tail` 或 Dashboard terminal 查看。
- agent 输入被接收后，Bot 会尽力给该条用户消息加 `THUMBSUP` reaction 表示 working；最终答复发出后会删除这个 reaction。没有 reaction 权限时主流程不受影响，只会在 worker log 里记录失败。
- 旧命令 `/where` 和 `/send <message>` 仍保留兼容，但主流程里不再需要。
- 同一个 Lark chat 内的命令会串行处理，避免连续输入并发打到同一个 session。

## 排障

机器人完全没反应：

- 确认 `staragent lark` worker 正在运行。
- 确认 Lark 事件订阅使用长连接 / WebSocket 模式。
- 确认已订阅 `im.message.receive_v1`。
- 确认应用权限已添加并发布版本。
- 私聊里确认消息是管理命令，例如 `/sessions` 或 `/help`。
- 需要和 agent 对话时，确认你在飞书对话群里先执行过 `@机器人 /use <node/session>`。
- 群聊里确认机器人是从群 `设置` / `群机器人` / `添加机器人` 加进去的，消息是 `@机器人 /command`。

返回 `StarAgent Lark access denied`：

- 检查 `STARAGENT_LARK_ALLOWED_USERS` 或 `STARAGENT_LARK_ALLOWED_CHATS`。
- 临时联调可用 `STARAGENT_LARK_ALLOW_ALL=1`，不要长期这样跑。
- 不需要提前知道自己的 user ID；先临时打开 `STARAGENT_LARK_ALLOW_ALL=1`，私聊发送 `/sessions`，再从 Worker Output 里复制 `open_id` 到 `STARAGENT_LARK_ALLOWED_USERS`。

`/open` 不返回链接：

- 设置 `STARAGENT_DASHBOARD_URL`。

远端 node 的普通消息或 `/tail` 失败：

- 确认 Lark worker 进程环境里有 `STARAGENT_NODE_TOKEN` 或 `STARAGENT_AUTH_TOKEN`。
- 确认 Hub 已配置该 node，且 node API 可达。
