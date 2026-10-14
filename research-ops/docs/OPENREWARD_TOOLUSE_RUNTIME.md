# OpenReward ToolUse runtime

ToolUse 使用现成的官方 ORS 环境
[`EnvCommons/toolathlon-gym`](https://github.com/EnvCommons/toolathlon-gym)，不是本仓库原来的
计数器 mock。固定版本为 `9c82859d05bc1f0376150d749cd2e3d43471d583`：503 个任务、25
个 bundled MCP server、每个 ORS session 独立 PostgreSQL clone 和 workspace，终止工具为
`claim_done`，官方 evaluator 返回二值 reward。

Orbitree 的 `/tooluse` 是 ORS-to-ORS proxy。它转发官方 prompt、任务动态工具、SSE
observation、reward 和 teardown；训练样本只保存 query 与 catalog selector，不保存 evaluator
源码、数据库种子或服务凭据。

## 构建与启动

先取得并固定官方源码：

```bash
git clone https://github.com/EnvCommons/toolathlon-gym.git /path/to/toolathlon-gym
git -C /path/to/toolathlon-gym switch --detach \
  9c82859d05bc1f0376150d749cd2e3d43471d583
```

构建脚本会再次校验 commit 和 tracked worktree。默认把上游的 `ubuntu:22.04` 换成已有的
`python:3.12-slim`，并对 uv 与 Node 22 使用固定版本和 SHA256 校验的构建输入，用于绕过
部分机器不可用的 Docker Hub/NodeSource mirror。该上游 commit 的并发 session server 会注入
`PGDATABASE=s_<session>`，但 468 个 task preprocess/evaluator 文件仍有 479 处硬编码
`toolathlon_gym`。构建脚本会在 image 内把这些连接点改为读取 `PGDATABASE`，并在改写后用
AST 拒绝任何未处理的精确数据库字面量；上游 checkout 保持 clean。最终 image 同时写入
`research-ops.upstream-revision` 与 `research-ops.task-db-patch-sha256` label。该版本的 Canvas
MCP 带有 PostgreSQL router，但没有接入 client，也没有声明 `pg` 依赖；原 client 反而会给
`CANVAS_DOMAIN` 无条件加 `https://` 并请求 ORS 根端口。构建时会显式安装 `pg/@types/pg`，在
存在 `PG_DATABASE` 时接入 per-session router，并保留显式 HTTP scheme 作为无数据库 fallback。
对应补丁摘要写入 `research-ops.canvas-http-patch-sha256` label，避免把 TLS 错误、404 或 MCP
进程退出当作策略失败；依赖修复另写入 `research-ops.canvas-pg-dependencies` label。

```bash
uv run python scripts/build_toolathlon_runtime.py \
  --source /path/to/toolathlon-gym \
  --image research-ops/toolathlon-gym:9c82859d

docker compose -f deploy/openreward-tooluse/compose.example.yaml up -d
export ORS_TOOLUSE_SERVICE_URL=http://127.0.0.1:9102
```

官方镜像较大：它会安装 PostgreSQL、Node、Playwright 和 25 个 bundled MCP server。上游
并行 Node 构建会隐藏单个后台失败，因此构建脚本会对 `google-forms-mcp` 再执行一次显式、
失败即中止的安装与编译，并写入 `research-ops.google-forms-build-fix` label。当前镜像在构建期
完整发现 25 个 server、441 个工具。若目标机器只能
访问内部 registry，应把构建后的 image 推到内部 registry，再通过 `ORS_TOOLUSE_IMAGE`
引用。上游 server 没有内建鉴权，因此 compose 默认只绑定 loopback；跨机器部署时必须放在
私网或经过鉴权反向代理。若代理需要 token，可在 backend contract 的 `auth_envs` 中只填写
token 的环境变量名。

## 空状态 control

先验证空数据库状态不会误得分：

```bash
uv run python scripts/run_openreward_tooluse_canary.py
```

canary 会先通过环境内的 `python_execute` 验证 `PGDATABASE == current_database == s_<session>`，
再直接调用 `claim_done`。验收条件是 `finished=true`、`reward=0.0`，且 evaluator 输出没有
PostgreSQL FATAL、连接失败、evaluation error 或 traceback。使用 `--all` 检查 8 条后还必须
确认 session 数据库残留为 0。之后再用 policy 完成同一任务；只有官方 evaluator 返回 `1.0`
才证明工具调用和状态写入完整。

8 条 selector 已生成在
`data/openreward_tooluse_canary_20260720/slime_selector.jsonl`。Slime 使用
`slime.rollout.openreward_rollout.generate` 时，每个 episode 创建一次 Toolathlon session；每个
MCP 调用只是同一 session 内的一轮，不重新创建环境。

Canvas MCP 与官方 evaluator 的确定性正向控制可单独运行：

```bash
uv run python scripts/run_openreward_tooluse_positive_canary.py
```

该控制通过官方 Canvas MCP 读取 session 数据、写入 Excel/Word workspace artifact，并要求官方
evaluator 返回 `reward=1.0`。evidence 明确记录 `policy_driven=false`；它证明环境闭环，不替代
真实 policy rollout。
