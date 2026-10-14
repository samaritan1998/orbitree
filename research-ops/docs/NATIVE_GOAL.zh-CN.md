# Native Goal

Orbitree 使用 Codex 的原生 `thread/goal/*` 接口。Web 的 Goal 与同一会话中 TUI `/goal` 读取的是同一份状态，自动继续和完成判断由 Codex 执行。

## 设置目标

1. 打开已运行过 Codex 的 Branch。
2. 点击 `Set goal`，填写目标。
3. 按需填写 Token budget。留空表示没有 Goal 专属 Token 上限。
4. 点击 `Start goal`。

目标文字原样传给 Codex。Orbitree 不拼接目标说明、不补写验收条件、不追加调度提示词，也不使用“再继续一轮”的消息循环。既有会话历史、仓库指令和所选身份保持不变。

目标绑定启动时的身份、模型和推理强度。修改“下一轮”的配置不会悄悄改变正在运行的 Goal。

## 查看进展

Results 顶部同时显示 Goal 状态和 Execution 状态。`Active` 只表示目标处于活动状态，不证明模型当前正在执行。

Goals 标签页显示最近检查时间、最近活动、最近输出、恢复次数、下一次重试，以及通知发送状态。Results 中的 Live activity 继续显示模型公开输出和工具活动，不显示隐藏推理。

| Execution | 含义 |
| --- | --- |
| Running | Codex 报告线程正在执行 |
| Waiting for next turn | 线程已加载，当前没有正在执行的轮次 |
| Runtime disconnected | 目标存在，但 Orbitree 尚未连接执行线程 |
| Recovering / Retry scheduled | 正在恢复连接，或等待已记录的重试时间 |
| Owned by another Codex process | 线程由其他进程持有，Orbitree 没有接管 |
| Execution failed | 执行或恢复失败，查看错误后可手动 Reconnect |

## 恢复与通知

- 默认每 30 秒巡检已发现的原生 Goal；检查本身不调用模型。
- 服务重启后，活动目标会重新加载原生线程并恢复事件订阅。已经暂停、受阻、完成或额度受限的目标不会仅因重启就被激活。
- 暂时性的连接、并发限流或服务端错误，在工具均已结束且结果明确时可以自动恢复。已完成的命令和文件修改不会阻止恢复；工具返回失败码但已有结束回执，也属于结果明确。恢复沿用原 Session 和既有工具结果，不重放工具调用或追加目标描述。
- 默认最多连续恢复 3 次，这是故障恢复次数，不是 Goal 的轮数或并发数上限。并发限流首次至少等待 60 秒，后续指数退避；其他瞬态错误使用配置的重试间隔。每次成功完成原生轮次后，连续故障次数归零。
- 不自动重试认证、额度、明确拦截和人工中断。工具仍在执行、缺少结束事件或状态不明确时，先核对结果，再手动 Resume。服务重启不会跳过这一检查。
- 升级前被旧规则阻止恢复的记录，只读取最近一个失败轮次的完整工具状态；轮次身份一致且工具结果均明确后，才安排自动重试。历史接口不可用或证据不完整时，不把缺失记录当作成功。
- 默认每 15 分钟发送一次活动目标的进展通知；状态变化、等待重试、执行失败及自动恢复也会写入如流通知队列。消息包含工作标题、Branch、Session ID 和页面链接。
- 通知队列持久化，发送失败会单独重试，不会重新执行 Goal。服务在发送成功与保存回执之间中断时，可能重复投递一条通知。
- 手动 Pause 不会被巡检撤销。暂停控制的是后续原生轮次；当前轮次和已经提交的外部任务不能仅凭 Goal 状态判断是否停止。

| 环境变量 | 默认值 | 用途 |
| --- | --- | --- |
| `ROPS_NATIVE_GOAL_SUPERVISION_ENABLED` | `true` | 启用后台巡检 |
| `ROPS_NATIVE_GOAL_CHECK_INTERVAL_SECONDS` | `30` | 巡检间隔 |
| `ROPS_NATIVE_GOAL_PROGRESS_INTERVAL_SECONDS` | `900` | 进展通知间隔 |
| `ROPS_NATIVE_GOAL_MAX_RECOVERY_ATTEMPTS` | `3` | 连续故障恢复次数 |
| `ROPS_NATIVE_GOAL_RETRY_DELAY_SECONDS` | `30` | 首次重试间隔，后续指数退避 |
| `ROPS_INFOFLOW_NOTIFICATIONS_ENABLED` | 取部署配置 | 启用如流通知；仍需已有机器人凭据 |

## 回归验证

普通回归不请求真实模型、不发送真实通知：

```bash
uv run pytest tests/test_native_goals.py tests/test_native_goal_supervision.py tests/test_turn_notifications.py tests/test_codex_cli.py tests/test_codex_app_server.py
```

真实 Codex 二进制协议回归使用临时 HOME、临时 CODEX_HOME 和本机模拟 Responses 服务，不读取实际登录凭据、不提交外部任务：

```bash
ROPS_RUN_NATIVE_GOAL_PROTOCOL=1 uv run pytest tests/test_native_goal_protocol.py
```

前端验收脚本使用模拟 API，覆盖桌面、平板、手机及目标原文校验：

```bash
npm --prefix web run build -- --outDir /tmp/research-ops-native-goal-dist
node scripts/check-native-goal-ui.mjs /tmp/research-ops-native-goal-dist
```

截图保存在 `/tmp/research-ops-native-goal-ui`。运行浏览器验收需要 Playwright Chromium；已有浏览器也可以通过 `PLAYWRIGHT_CHROMIUM_EXECUTABLE` 指定。

接口依据：[Codex App Server 官方文档](https://learn.chatgpt.com/zh-Hans/docs/app-server)。
