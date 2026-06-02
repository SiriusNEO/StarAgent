# StarAgent Feishu Integration

StarAgent 的飞书接入是一个轻量 command adapter。飞书只作为通知和遥控入口，StarAgent Hub / Node / tmux session 仍然是唯一的 session source of truth。

当前实现不改 Web 层，不引入 Feishu-owned session，也不会自动从飞书创建 agent session。

## 能力范围

支持命令：

```text
/sessions
/status <node/session>
/tail <node/session> [lines]
/send <node/session> <message>
/open <node/session>
```

在群里通常写成：

```text
@StarAgent /sessions
@StarAgent /send local/my-session 帮我继续修这个问题
```

也支持不带斜杠的前缀形式：

```text
staragent sessions
```

## 安装

```bash
pip install -e '.[feishu]'
```

该 optional extra 会安装飞书官方 Python SDK `lark-oapi`。

## 飞书应用配置

在飞书开放平台创建一个企业自建应用：

1. 创建应用，记录 `App ID` 和 `App Secret`。
2. 启用机器人能力。
3. 权限管理中添加消息相关权限。
4. 事件订阅选择长连接 / WebSocket 模式。
5. 订阅事件 `im.message.receive_v1`。
6. 发布应用版本，并把机器人添加到目标群或私聊中。

建议的最小权限：

```text
im:message
im:message:send_as_bot
im:message.group_at_msg:readonly
im:message.p2p_msg:readonly
```

说明：

- `im.message.receive_v1` 是接收用户消息的事件。
- 群聊建议用 `@机器人 /command` 触发。
- 如果只打算在群里使用，可以先不开私聊消息权限。
- 当前 StarAgent 不需要文件、图片、卡片、群管理权限。

## 环境变量

必填：

```bash
export STARAGENT_FEISHU_APP_ID='cli_xxx'
export STARAGENT_FEISHU_APP_SECRET='xxx'
```

访问控制至少配置一种：

```bash
# 推荐：限制到可信群
export STARAGENT_FEISHU_ALLOWED_CHATS='oc_xxx,oc_yyy'

# 或者：限制到可信用户，支持 open_id / user_id / union_id
export STARAGENT_FEISHU_ALLOWED_USERS='ou_xxx,on_xxx'

# 仅测试用：放开所有飞书发送者
export STARAGENT_FEISHU_ALLOW_ALL=1
```

可选：

```bash
# /open 命令返回的 Dashboard 链接前缀
export STARAGENT_DASHBOARD_URL='https://staragent.example.com'

# 如果飞书应用配置了 verification token / encrypt key
export STARAGENT_FEISHU_VERIFICATION_TOKEN='xxx'
export STARAGENT_FEISHU_ENCRYPT_KEY='xxx'

# 如果需要操作远端 node，worker 环境里也要有 node token
export STARAGENT_NODE_TOKEN='<same token as nodes>'
# 或复用
export STARAGENT_AUTH_TOKEN='<hub token>'
```

## 启动

前台运行：

```bash
staragent feishu
```

tmux 常驻运行：

```bash
tmux new -ds staragent-feishu 'staragent feishu'
```

也可以直接通过参数传入配置：

```bash
staragent feishu \
  --app-id cli_xxx \
  --app-secret xxx \
  --allowed-chats oc_xxx \
  --dashboard-url https://staragent.example.com
```

## 使用示例

列出所有 session：

```text
@StarAgent /sessions
```

查看状态：

```text
@StarAgent /status local/my-session
```

查看终端尾部输出：

```text
@StarAgent /tail local/my-session 120
```

向 agent session 发送一条 Chat 消息：

```text
@StarAgent /send worker-1/codex-login-fix 继续跑测试并修掉失败项
```

打开 Dashboard session 页面：

```text
@StarAgent /open worker-1/codex-login-fix
```

## 当前边界

- 不自动创建 StarAgent session。
- 不做飞书 thread 到 StarAgent session 的持久绑定。
- 不做 Feishu 卡片、文件、截图、审批流。
- `system` session 只读，`/send` 只允许发给 `agent` session。
- 同一个飞书 chat/thread 内的命令会串行处理，避免连续 `/send` 并发打到同一个 session。

## 排障

机器人完全没反应：

- 确认 `staragent feishu` worker 正在运行。
- 确认飞书事件订阅使用长连接 / WebSocket 模式。
- 确认已订阅 `im.message.receive_v1`。
- 确认应用权限已添加并发布版本。
- 确认机器人已添加到群里。
- 群聊里确认消息是 `@机器人 /command`。

返回 `StarAgent Feishu access denied`：

- 检查 `STARAGENT_FEISHU_ALLOWED_USERS` 或 `STARAGENT_FEISHU_ALLOWED_CHATS`。
- 临时联调可用 `STARAGENT_FEISHU_ALLOW_ALL=1`，不要长期这样跑。

`/open` 不返回链接：

- 设置 `STARAGENT_DASHBOARD_URL`。

远端 node 的 `/send` 或 `/tail` 失败：

- 确认 Feishu worker 进程环境里有 `STARAGENT_NODE_TOKEN` 或 `STARAGENT_AUTH_TOKEN`。
- 确认 Hub 已配置该 node，且 node API 可达。

