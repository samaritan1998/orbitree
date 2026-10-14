# OpenReward 四域运行时协议

系统搭建与真实运行时解耦。仓库内的 ORS Server、session、task、tool、SSE、reward 和
`finished` 语义不依赖 AgentOS 地址、模型 checkpoint、sandbox 镜像或 Office 服务。
没有这些材料时使用 `mock` 模式完成协议和训练 harness 联调；部署时切换到 `contract`
模式，由环境变量注入真实引用。

## 对训练侧的统一接口

四个环境都使用同一套 Open Reward Standard HTTP/SSE 接口：

```text
POST /create_session
POST /create
GET  /{environment}/prompt
GET  /{environment}/task_tools
POST /{environment}/call
POST /delete
```

工具返回统一为：

```json
{
  "blocks": [{"type": "text", "text": "observation"}],
  "reward": null,
  "finished": false,
  "metadata": {}
}
```

训练器只理解这个接口，不直接理解 Datawheel、Docker、Office 或业务 API。

## 对真实 SWE/PPT/ToolUse 服务的 bridge 协议

真实服务可以使用任意实现或镜像，只需由服务本身或一个轻量 sidecar 实现下面的 JSON
协议。每个 endpoint 的 method/path 由
`config/openreward-backends.example.yaml` 配置。

创建 session：

```json
{
  "environment": "swe",
  "task_id": "task-001",
  "task_spec": {"query": "..."}
}
```

创建响应至少包含一种 session ID；也可以同时返回 task-specific tools：

```json
{
  "session_id": "runtime-session-001",
  "tools": [
    {
      "name": "lookup",
      "description": "Look up one record",
      "input_schema": {"type": "object", "properties": {}}
    }
  ]
}
```

执行 action：

```json
{
  "environment": "swe",
  "session_id": "runtime-session-001",
  "task_id": "task-001",
  "action": "bash",
  "input": {"command": "pytest -q"}
}
```

执行响应直接返回 `BackendResult`，或包装在 `output` 字段中：

```json
{
  "output": {
    "blocks": [{"type": "text", "text": "tests passed"}],
    "reward": 1.0,
    "finished": true,
    "metadata": {"exit_code": 0}
  }
}
```

`submit`、`submit_deliverable` 等 verifier action 如果省略 `finished`，bridge 会按配置将其
设为 `true`。运行时必须为每个 session 提供 teardown；ORS 删除或 TTL 回收 session 时会
调用对应的 `DELETE` operation。

## 四域映射

| ORS 环境 | session backend | actions | 可选 task evaluator |
|---|---|---|---|
| `miaoda` | Datawheel/AgentOS | `start_rollout`, `poll_rollout` | Goblin Reward 回填 |
| `swe` | 可销毁 sandbox | `bash`, `read_file`, `write_file`, `submit` | verifier/test score |
| `ppt` | task-bundle PPT sandbox | `bash`, file tools, `render`, `submit_deliverable` | 四维 PPT rubric |
| `tooluse` | Toolathlon-Gym ORS session | 动态 MCP tools, `claim_done` | 官方二值 evaluator |

纯 MOPD 中上表最后一列都不进入训练 loss；Slime 的通用数值 reward 字段固定为 `0.0`，
训练信号来自对齐的 behavior/teacher token logprob。reward-backed RL 或混合 RL + OPD 才会
等待 evaluator 并消费其 task reward。

Miaoda 不经过通用 HTTP bridge：它已有专门的异步
`MiaodaDatawheelGoblinBackend`。reward-backed 模式由它负责 Datawheel artifact、Goblin job
和 reward join；`training_objective=pure_opd` 时则在 AgentOS rollout 完成后直接结束 episode，
明确返回 `task_reward_used=false`，不会提交 Goblin job。

## 启动方式

无需任何外部材料的完整四域 mock：

```bash
uv run python scripts/run_openreward_mock_e2e.py
uv run python scripts/run_openreward_server.py \
  --mode mock \
  --task-catalog config/openreward-tasks.example.yaml
```

拿到真实运行时后：

```bash
uv run python scripts/check_openreward_backend_contract.py --check-env
uv run python scripts/run_openreward_server.py \
  --mode contract \
  --backend-config config/openreward-backends.example.yaml \
  --task-catalog /path/to/tasks.yaml
```

可以按域逐个接入。例如第一阶段只连接真实 Miaoda，其余三个环境继续使用 mock：

```bash
uv run python scripts/check_openreward_backend_contract.py \
  --domain miaoda --check-env
uv run python scripts/run_openreward_server.py \
  --mode contract \
  --real-domain miaoda \
  --task-catalog /path/to/tasks.yaml
```

也可以集成到 Orbitree 主服务：

```bash
export ROPS_OPENREWARD_ENABLED=true
export ROPS_OPENREWARD_BACKEND_MODE=contract
export ROPS_OPENREWARD_BACKEND_CONFIG=./config/openreward-backends.example.yaml
export ROPS_OPENREWARD_TASK_CATALOG=/path/to/tasks.yaml
export ROPS_OPENREWARD_REAL_DOMAINS='["miaoda"]'
# Optional: the value stays in OPENREWARD_SERVER_API_KEY, only its name is configured here.
export ROPS_OPENREWARD_API_KEY_ENV=OPENREWARD_SERVER_API_KEY
uv run rops serve
```

此时 ORS base URL 为 `http://<host>:8020/openreward`。

## 安全约束

- backend YAML 和 task catalog 只能保存非敏感结构与环境变量名。
- credential 值在每次 HTTP 请求时从环境读取，不进入 ORS state、metadata 或 readiness。
- 可选 server API key 同时兼容官方客户端的 `X-API-Key` 和 Bearer header。
- task catalog 出现 token、password、JWT、signed URL 等字段时拒绝启动。
- 远程响应中的 credential-like metadata 会被删除，已配置 credential 值会从文本中脱敏。
- `runtime_mode=image` 只描述部署需求；具体容器调度由部署平台注入，不允许 ORS Server
  自行执行任意镜像或 shell。
