# OpenReward/ORS 统一环境接入

本仓库现在有一个不依赖 `openreward` Python SDK 的 ORS HTTP/SSE 实现，位于
`backend/research_ops/openreward`。它遵循 Open Reward Standard 的环境边界：

```text
task/split -> create_session -> create episode
          -> prompt + tools -> call(tool)
          -> ToolOutput(blocks, reward, finished) -> delete
```

四个环境名称固定为：`miaoda`、`swe`、`ppt`、`tooluse`。它们共用同一套
session、task、prompt、tool-call、SSE 和 reward 生命周期；领域差异只存在于
backend 和 tool schema。

Miaoda 的 canonical online episode 不是一个 `chat_round`，而是：

```text
start_rollout
  -> Datawheel/AgentOS running
  -> AgentOS result artifact
  -> Goblin Reward pending/running
  -> poll_rollout: finished=true, reward=<scalar>
```

`miaoda_step` 仍保留用于 direct EntryPoint 调试，但不应把它当作 MOPD 的
token-level turn。

这里有两个容易混淆的“在线”入口：

```text
直接调试/逐轮观测：workspace-be 创建 app/conversation
                  -> EntryPoint A2A 或 chat_round

训练 canary/完整控制面：Datawheel API
                  -> DB/Celery/worker/AgentOS SDK
                  -> 同一个 EntryPoint/workspace-be
                  -> 下载 artifact -> Goblin Reward
```

两条路径最终使用同一套 AgentOS 执行服务。一个 query 对应一个 app/conversation
和一个 episode；一个 episode 内可以有多次 `chat_round` 以及大量内部模型/tool
调用，但不应为每个 token 或每个内部调用重新创建 rollout。

`chat_round` 返回的 `agent_state` 与 A2A/HTTP 的 `status.state` 是两层状态，不能
只看 `final=true`：

| 原始状态 | 含义 | 完整 Miaoda episode 是否 finished |
|---|---|---|
| `loading`/`running`/`working`/`submitted` | 仍在执行 | 否 |
| `awaiting_user_input` / `input-required` | 需求阶段边界，等待“立即创作” | 否（应继续同一 app） |
| `finished` + `status.state=completed` | coding 生成结束，待 artifact/reward | 执行层是；训练层等 Goblin reward |
| `stopped` | 已停止，但成功语义不稳定 | 只作终态；需检查 artifact，不直接当成功 |
| `error`/`failed`/`rejected`/`cancelled` | 执行失败或取消 | 是失败终态 |

`status.state=completed` 只表示传输/任务结束，仍要结合 `agent_state` 和 artifact
判定；`final=true` 本身不是成功信号。

## 本地冒烟

```bash
uv run python scripts/run_openreward_local.py --demo-tasks
uv run python scripts/run_openreward_mock_e2e.py
curl http://127.0.0.1:8080/health
curl http://127.0.0.1:8080/list_environments
```

Research Ops 主服务中也可以显式打开同一协议（默认关闭）：

```bash
export ROPS_OPENREWARD_ENABLED=true
uv run rops serve
# ORS base URL: http://127.0.0.1:8020/openreward
```

默认 registry 使用四个安全的领域 mock，不会执行 shell、不会创建 AgentOS 应用、
不会启动 Office，也不会访问外部服务：

- Miaoda：模拟 `rollout -> Goblin pending -> reward`；
- SWE：使用 session 内存文件系统和确定性 verifier；
- PPT：使用内存 artifact，并返回一个有效的 ORS `ImageBlock` 模拟渲染；
- ToolUse：使用 session 隔离的工具注册表和目标状态 verifier。

所有工具调用先按公开的 `input_schema` 校验。生产启动时应通过
`build_default_environments(backends=...)` 注入真实 backend；不要把 token 放入 task
spec、metadata 或日志。

## ORS 与四类任务的映射

| 环境 | 初始 task 至少包含 | 主要工具 | 后端负责的状态与 reward |
|---|---|---|---|
| `miaoda` | `query`、可选资产/skill 引用 | `start_rollout`、`poll_rollout`（调试：`miaoda_step`） | Datawheel/AgentOS 完整应用、Goblin 异步 reward、episode join |
| `swe` | repo/issue/commit 基线、测试入口 | `bash`、`read_file`、`write_file`、`submit` | 隔离 sandbox、补丁、测试/ verifier、资源上限 |
| `ppt` | PRD/query、模板/参考 slide、输出约束 | `ppt_read`、`ppt_write`、`render`、`submit_deliverable` | SlideForge/Office 运行时、渲染图、结构/视觉/内容评分 |
| `tooluse` | 工具目录、初始状态、目标状态 | `invoke_tool`、`submit` | 工具服务会话、数据库/状态转移、成功判定 |

`PptEnvironment` 的参考图片通过 ORS `ImageBlock` 返回，因而不需要额外的
PPT 专用传输协议。SWE 的容器/VM 也不是 ORS 本身的一部分，而是 environment
backend 持有的 sandbox 生命周期。

## 真实 backend 接口

每个 backend 只需要实现以下三个方法（可以是同步或 async）：

```python
def setup(*, task_spec, secrets, state): ...
def execute(*, action, payload, task_spec, state) -> BackendResult: ...
def teardown(*, task_spec, state): ...
```

`BackendResult` 对应 ORS `ToolOutput`：`blocks`、可选 `reward`、`finished`、
`metadata`。`state` 是单个 ORS session 的可变状态，不应跨 session 共享。

如果内部服务还没有正式 SDK，可以先用 `CallbackBackend` 注入一个 RPC/HTTP
调用函数；等接口稳定后再替换成专用 backend，不需要改 ORS server 或训练侧。

仓库现在也提供可配置的 `RemoteServiceBackend`。SWE、PPT、ToolUse 可以直接从
backend YAML 装配远程 session/action/teardown，不要求在 Research Ops 中引入服务方
SDK。统一 JSON bridge 详见
[`OPENREWARD_RUNTIME_PROTOCOL.md`](OPENREWARD_RUNTIME_PROTOCOL.md)。真实地址和凭据仅在
`contract` 模式启动时解析，搭建与测试整个四域系统不依赖这些材料。

真实后端材料使用严格的、只保存环境变量名的契约文件：

```bash
# 只校验结构并列出需要的环境变量名；不会显示任何值
uv run python scripts/check_openreward_backend_contract.py

# 部署前同时检查运行时引用是否齐全；缺失时退出码为 1
uv run python scripts/check_openreward_backend_contract.py --check-env
```

模板位于 `config/openreward-backends.example.yaml`。配置无法写入 token、key 或地址值，
只能引用运行时环境变量名；readiness 输出也只包含缺失的变量名。

### Miaoda/AgentOS + Goblin

`MiaodaDatawheelGoblinBackend` 已实现第一版完整状态机。它使用现有的
`AgentOSClient` 提交 Datawheel batch、下载结果文件，再使用
`GoblinRewardClient` 上传并轮询 Reward job。需要明确的外部契约是：

1. Datawheel API 能接受原始 query，并返回稳定的 `batch_task_id`；
2. batch 状态能区分 `running/completed/failed`，并能下载结果 artifact；
3. Goblin job 能返回可访问的 `result_file`；
4. Goblin 结果中至少有一个可和 `episode_id/case_id/app_id` 对齐的 ID，以及明确的
   数值 reward 列。

Datawheel 结果表通常还会给出 `conv_id` 和“跑批单条记录 id”；backend 会把这些
非敏感 ID 暂存为 Goblin join 候选，避免生成成功但 reward 无法回填。

当前 evaluation 配置中观察到的非敏感地址（提交前仍需确认跑批 worker 的网络可达性）是：

```text
AGENTOS_ENV=evaluation
AGENTOS_WORKSPACE_BASE_URL=http://198.51.100.239
AGENTOS_ENTRYPOINT_BASE_URL=http://evaluation.example.com
ROPS_AGENTOS_DATAWHEEL_BASE_URL=http://198.51.100.250:80/api/v1/datawheel
ROPS_GOBLIN_BASE_URL=http://198.51.100.139:8118
```

这些 endpoint 名称是本项目 adapter 的配置约定，不是 AgentOS 强制的环境变量名；
单独运行 canary 脚本时 `DATAWHEEL_API_BASE` 是同一 Datawheel 地址的别名。
只读直连路径需要 `AGENTOS_WORKSPACE_JWT_KEY`；Datawheel 路径由 worker 负责
workspace/EntryPoint 鉴权，只需在调用方运行时注入 `DATAWHEEL_TOKEN`。模型 key 和
Goblin token 分别只通过 `AGENTOS_MODEL_API_KEY`、`GOBLIN_REWARD_API_TOKEN` 注入。

初始化时只传环境变量名，不传 secret 值：

```python
import os

from research_ops.agentos import AgentOSClient
from research_ops.openreward import (
    MiaodaDatawheelGoblinBackend,
    MiaodaOnlineConfig,
)
from research_ops.reward import GoblinRewardClient

datawheel = AgentOSClient(
    os.environ["ROPS_AGENTOS_DATAWHEEL_BASE_URL"],
    token_env="DATAWHEEL_TOKEN",
)
goblin = GoblinRewardClient(
    os.environ["ROPS_GOBLIN_BASE_URL"],
    token_env="GOBLIN_REWARD_API_TOKEN",
)
backend = MiaodaDatawheelGoblinBackend(
    datawheel,
    goblin,
    MiaodaOnlineConfig(
        generated_user_id="<runtime-generated-user>",
        model_name="<policy-checkpoint>",
        model_api_url="<mopd-gateway>/v1/chat/completions",
        model_api_key_env="AGENTOS_MODEL_API_KEY",
        benchmark_dataset_name="miaoda-online-mopd-train-v1",
    ),
)
```

`generated_user_id`、checkpoint 名称和 URL 是路由参数；Datawheel/Goblin/model
token 仍由运行时 secret store 提供，不能写进 task、metadata 或提交记录。

当前仓库已有 `AgentOSEntrypointClient` 和 direct continuation backend，后者只用于
定位 AgentOS 问题。真实训练应让 AgentOS 内部通过
`codingPromptiModelConfig.api_url` 调用 MOPD inference gateway；Gateway 负责记录
token ids/logprobs，Datawheel/AgentOS 负责完整应用执行，Goblin 负责 terminal reward。

评估器故障不能静默转成 reward 0：AgentOS 真实执行失败可以是有效的 0 分 episode，
但 Goblin Pod/网络/结果文件故障必须标记 `usable=false`，由训练器丢弃或重试。

Goblin 当前部分任务只在每个 case 的 `rollout.msg` 中返回“优秀 / 基本可用 /
不可用”文字评级，而没有数值 reward。ORS 会保留原始 `reward_label`，并返回
`scalar_reward_available=false`、`usable=false`；在官方数值映射或数值字段落地前，
不得自行映射为 0/0.5/1。每个 Datawheel batch 使用独立的 Goblin workdir，避免
`resume` 误读其他 episode 的历史评级。

共享路径模式还要求 `GOBLIN_REWARD_RESULT_ROOT` 是绝对 CFS 路径。相对路径会被
Goblin worker 解释为其自身容器内的路径，任务虽然可能显示 `completed`，但
Research Ops 无法读取 `result.jsonl` 或 `rollout.msg`；配置对象会在启动时拒绝这种
设置。

### 早期 bridge canary 记录（2026-07-19）

SWE、PPT、ToolUse 曾通过隔离 mock 完成各 1 条协议 canary，三者 ORS terminal reward
均为 `1.0`。这些结果只证明 HTTP bridge：旧 SWE 是人工 `calc.py` + unittest，旧 PPT
只检查 LibreOffice 渲染与页数，旧 ToolUse 是计数器目标状态，都不是训练级 benchmark
reward，不得作为真实环境成功率。历史证据集中在
`data/real_four_domain_20260719/ors_e2e_evidence.json`，PPT 渲染图在同目录的
`ppt_images/`。

SWE 后续已升级为官方 SWE-bench 4.0.4，并用 empty/gold patch 对照证明 reward 可区分；
ToolUse 已接入固定 revision 的 Toolathlon-Gym，并增加 session 数据库兼容补丁与健康探针；
PPT 已增加 task bundle、干净渲染和四维 rubric
硬门槛。各域以各自 runtime 文档中的最新证据为准。

Miaoda 使用真实 Datawheel/AgentOS 完成了 batch
`fba4506f-09af-42f3-82f4-951169a87b91`（1/1 completed），并通过 Goblin job
`d398be9e` 在共享 CFS 上重新评分。该样例的 Goblin 结论是 `不可用`，结果文件没有
标量 reward，ORS 因此正确返回 `reward=null`、`reward_label=不可用`、
`usable=false`；这不是把评测失败伪装成零分。完整产物和解析证据见
`data/real_four_domain_20260719/miaoda_goblin/rescore-0edf8301/` 与
`data/real_four_domain_20260719/miaoda_e2e_evidence_r2.json`。在 Goblin 提供官方
数值字段或明确标签映射前，该样例不能直接进入需要标量 reward 的 MOPD trainer。

### SWE

真实 SWE runtime、provider 边界、隔离策略和启动命令见
[`OPENREWARD_SWE_RUNTIME.md`](OPENREWARD_SWE_RUNTIME.md)。`swebench`、`swegym` 与
`r2e` 由各自 verifier provider 执行；其中 R2E 使用内部 task image 和 manifest
evaluator，不得解释为官方 SWE-bench score。

### PPT

隔离 sidecar、task bundle 目录、policy/verifier image、rubric 协议和启动命令见
[`OPENREWARD_PPT_RUNTIME.md`](OPENREWARD_PPT_RUNTIME.md)。当前 8 条 prompt 的 provenance
已确认，但原 `/inputs` 资产不在 Warehouse；在恢复 task bundle 并接入训练级 rubric 前，
结构通过的 deck 会返回 `reward=null, usable=false`，不会冒充训练 episode。

### ToolUse

使用官方 `EnvCommons/toolathlon-gym` 固定 commit；部署、ORS proxy 和空状态 control 见
[`OPENREWARD_TOOLUSE_RUNTIME.md`](OPENREWARD_TOOLUSE_RUNTIME.md)。每个 episode 独立 clone
PostgreSQL 与 workspace，模型只看到任务动态工具，`claim_done` 调用官方 evaluator 返回
二值 reward。构建阶段的数据库兼容补丁及其 SHA 会写入 image label；当前 8 条 canary 的
session DB/evaluator 健康检查通过，25 个 MCP server、441 个工具已在构建期完整发现。
MCP/API 凭据仍留在环境服务侧，不进入 ORS task 或轨迹。

## MOPD 训练边界

在线 rollout、teacher、optimizer、权重刷新和版本校验的稳定五阶段闭环见
[`OPENREWARD_ONLINE_MOPD.md`](OPENREWARD_ONLINE_MOPD.md)。

ORS 定义的是 environment-side 的 task、tool、observation、reward、termination，
不定义模型 token、token id 或 behavior log-prob。若要做严格的 on-policy MOPD，仍需
让 rollout inference gateway/SGLang/训练框架记录每次模型输出的 token ids 和 logprobs，
再把它们与 ORS session/turn/task_id 关联。仅有 AgentOS 的历史 trajectory 可以做
环境 episode/离线分析，但不能凭空补出 token-level MOPD 样本。

## 推荐的 Miaoda 8 条 canary 时序

```text
1. 从 ORS train split 取 8 个 query，每个生成唯一 episode_id
2. 一次 Datawheel API batch 提交 8 条 query（不是每个模型 turn 一个任务）
3. AgentOS Worker 执行应用；模型请求路由到 MOPD gateway
4. batch completed 后下载 result_raw.xlsx
5. 一个 Goblin job 评估这批结果
6. 轮询 Goblin，读取 result_file
7. 按 episode_id/case_id/app_id join reward 与 gateway trajectory
8. 只把 usable=true 的 episode 送入 MOPD trainer
```

当前 backend 默认是“一 session 一 Datawheel row”，方便先做契约验证；扩展到 8/32
条时应在 backend 外层加 batch coordinator，共享一次 Datawheel/Goblin job，而不改变
ORS 的 `start_rollout/poll_rollout` 协议。

## 需要外部提供的接入材料

先不需要镜像或凭据即可完成协议契约测试。进入真实四域 backend 时，按下面最小清单
提供“接口说明/镜像名/运行方式”，凭据仍通过 secret manager 或环境变量注入：

- Miaoda：正式 batch 路径不需要新镜像，也不要求调用方直接持有 workspace-be /
  EntryPoint 配置；需要 Datawheel endpoint、generated user、policy gateway、Goblin
  endpoint，以及 Goblin `result_file` 的共享路径或下载方式。workspace-be / EntryPoint
  只用于单条直连诊断；
- SWE：已具备 Kubernetes sidecar；不同 provider 需要各自固定 task image 与 verifier
  manifest，`cluster-d` 正式任务不依赖本地 Docker；
- PPT：仍需原造数/SlideForge task bundle（恢复每条 `/inputs`）、批准的 PPT skill 或原
  policy image，以及返回 grounding/structure/visual/editability 的 rubric service；
- ToolUse：环境代码和 verifier 由官方 Toolathlon-Gym 提供；部署机需构建/拉取固定 revision
  与已记录兼容补丁的 image，若跨机器访问则增加私网鉴权代理；
- 训练侧：模型 inference endpoint（最好兼容 OpenAI API）、token/logprob 返回能力，
  以及要使用的 Slime/MOPD rollout harness 版本。

这些材料齐备后，可以先接 `miaoda` 的 1 条 canary，再扩到 8 条；PPT、SWE、ToolUse
分别使用自己的 ORS task split 和 backend，不需要为每个工具调用重新创建 AgentOS/Datawheel
批任务。
