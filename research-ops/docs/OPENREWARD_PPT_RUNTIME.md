# OpenReward PPT runtime

PPT 是自建环境，不能用“生成了一个 `.pptx` 且页数正确”作为训练 reward。本仓库提供的
sidecar 把 PPT task 映射为一个隔离 session：

```text
ORS /ppt
  -> PPT sidecar
     -> policy container: /inputs (只读任务包) + /workspace (可写)
     -> clean verifier container: LibreOffice -> PDF -> 逐页 PNG
     -> PPT rubric service: grounding / structure / visual / editability
```

policy 执行单元没有宿主 workspace 或网络，也不能访问 Docker socket；本地开发可以由
sidecar 调 Docker，正式环境则由 Kubernetes runtime 创建 policy/render Pod。渲染始终在
第二个干净执行单元中完成，避免 policy 替换渲染器或伪造截图。terminal 规则如下：

| 结果 | reward | `usable` |
|---|---:|---:|
| 缺失、损坏、不可编辑、页数错误或无法由干净容器渲染 | `0.0` | `true` |
| 结构和渲染通过，但 rubric 未配置或服务故障 | `null` | `false` |
| SlideForge 完成评分，但 `training_usable=false` | `null`（诊断分保留在 metadata） | `false` |
| rubric 完成四个维度并返回官方标量 | `[0,1]` | `true` |

因此 rubric 基础设施故障不会被静默当成模型 0 分。

## Task bundle

每个 catalog task 必须能恢复原始 `/inputs`，目录约定为：

```text
$ORS_PPT_TASK_BUNDLE_ROOT/
  <task_id>/
    inputs/
      references/...
      style_refs/...
      current_pptx/...
    rubric.json
    rubric_input.json
    task.json
    historical/
      slides.pptx
      evaluation.json
      rendered/...
```

当前 catalog 固定为 0629 生产 run 的 8 条历史 `training_usable=true` 任务，覆盖
`style_reference/create`、`document_grounded/create`、`open/create` 和 `open/edit`。资产、
预生成 rubric 和历史正样本均已物化，不再依赖原先缺失的 prompt-only task ID。重新物化：

```bash
uv run python scripts/materialize_slideforge_ppt_canary.py \
  --run-root /absolute/path/to/demo-ppt-source
```

sidecar 在创建 session 前逐个检查 catalog 声明的资产；缺一个文件就拒绝创建。物化后检查：

```bash
uv run python scripts/check_ppt_task_bundles.py \
  --catalog config/openreward-ppt-canary.json \
  --bundle-root data/example-ppt-bundles/bundles
```

## Policy/verifier image

真实 canary 使用原生产镜像：

```text
registry.example.com/acg-agi/ppt-rollout:0610-deps
sha256:6c1ba5a3aca026d001a931bf494716c4ef1ebb6f963df1923d96fa7fec00504b
```

该镜像包含固定版本的 PPTX/PDF skill、Node/PptxGenJS、Python PPTX、MarkItDown、
LibreOffice、Poppler、中文字体、Claude Code 和 ccrelay。设置
`ORS_PPT_SKILL_IN_IMAGE=true`；模型和 Reward key 仅通过部署环境注入。

`0610-deps` 不包含 `SlideForgePipeline/pipeline/stages/reward` 源码。Reward adapter
启动时会校验现有 checkout 中的 `build_candidate_bundle.py`、
`run_reward_with_claude_skill.sh` 和 `validate_evaluation.py`，compose 将该 checkout
只读挂载到 `/opt/slideforge`。不要把模型 key、代理凭据或 SlideForge 生产数据复制进镜像。

仓库仍保留一个不含私有 skill 的通用 verifier/control image：

```bash
docker build \
  -f deploy/openreward-ppt/policy.Dockerfile \
  -t research-ops/ppt-policy:20260720 .
```

Dockerfile 默认使用 Debian 的清华镜像并继续校验 Debian archive 签名；生产环境可通过
`--build-arg DEBIAN_MIRROR=... --build-arg DEBIAN_SECURITY_MIRROR=...` 切换到批准的内部镜像。
Python 包默认使用清华 PyPI 镜像，也可通过 `--build-arg PIP_INDEX_URL=...` 改为内部索引。

不要把模型 key 或 signed asset URL 烘焙进任何 image。

## Rubric service contract

设置 `ORS_PPT_RUBRIC_URL` 后，sidecar 调用：

```text
POST /v1/evaluate
```

请求使用共享路径传递 task、`slides.pptx`、逐页 PNG 和对应 inputs root。响应必须是：

```json
{
  "usable": true,
  "reward": 0.73,
  "grader_id": "slideforge-ppt-reward-v1",
  "rubric": {
    "content_grounding": 0.8,
    "structure": 0.9,
    "visual_quality": 0.5,
    "editability": 1.0
  },
  "metadata": {
    "slideforge_domain_scores": {
      "Requirement Compliance": 0.9,
      "Substance Quality": 0.75,
      "Presentation Design Quality": 0.82
    },
    "evaluation_confidence": {
      "training_usable": true,
      "evaluation_valid": true
    },
    "dimension_derivation": {}
  }
}
```

adapter 复用 task bundle 中的 `rubric.json`，调用现有 `build_candidate_bundle.py` 和
`run_reward_with_claude_skill.sh`，并按 task/artifact/rubric hash 缓存结果。`reward` 始终保留
SlideForge 官方标量；四维从 item-level 结果计算：grounding 取 source-faithfulness / hallucination
等条目，structure 取非 grounding 的 requirement 条目，visual 取 design 条目，editability 由
`SYS_OPENABLE_FILE`、`SYS_EDITABILITY` 和 ORS 的 OOXML inspection 共同决定。原始三项 domain
score 仅作为 metadata 保留。

单条完整 Reward 可能超过 15 分钟；runtime 和 adapter 默认都配置为 5400 秒超时，adapter
默认并发为 1，以避免现有 ccrelay 固定端口在同一容器内冲突。统一 ORS 的 PPT backend
timeout 配置为 5700 秒，必须高于 rubric timeout。

四个维度缺一不可。若 SlideForge 返回 `training_usable=false`，sidecar 会保留 diagnostic reward
和评分 metadata，但向训练返回 `reward=null, usable=false`。`ORS_PPT_RUBRIC_TOKEN` 只在
sidecar 和 adapter 环境注入，不进入 task 或 trajectory。

## Kubernetes 正式运行方式

正式链路完全在 Kubernetes 中运行，不依赖提交端的 Docker daemon，也不会在 runtime
Pod 内挂 Docker socket。Reward adapter 和 PPT runtime 是两个独立 Deployment：前者
从 CFS 只读挂载 Orbitree、SlideForgePipeline 和 8 条 task bundle；后者通过
ServiceAccount 按 episode 创建隔离的 policy Pod 和 clean-renderer Pod。模型 key 引用
专用的 `openreward-ppt-model` Kubernetes Secret；部署脚本在集群内生成 `runtime_token`
与 `rubric_token`，只保存到 `openreward-ppt-auth` Secret，不打印明文：

```bash
uv sync --extra dev --extra ppt-k8s

uv run python scripts/deploy_openreward_ppt_k8s.py \
  --kubeconfig /absolute/path/to/rl-training.kubeconfig \
  --namespace rl-training \
  --slideforge-root /absolute/path/to/SlideForgePipeline

uv run python scripts/deploy_openreward_ppt_k8s_runtime.py \
  --kubeconfig /absolute/path/to/rl-training.kubeconfig \
  --namespace rl-training
```

集群内 Reward 地址为：

```text
http://openreward-ppt-reward.rl-training.svc.cluster.local:9104
```

runtime 地址为：

```text
http://openreward-ppt-runtime.rl-training.svc.cluster.local:9103
```

先用独立 K8s Job 跑一条 policy-driven control episode，验证生成、独立渲染和真实
Reward 的端到端结果：

```bash
uv run --extra ppt-k8s python scripts/run_openreward_ppt_k8s_control_job.py \
  --kubeconfig /absolute/path/to/rl-training.kubeconfig \
  --namespace rl-training
```

再用独立 K8s Job 对历史合格 PPT 做 HTTP parity；两类 Job 都不依赖提交端持续在线：

```bash
uv run --extra ppt-k8s python scripts/run_openreward_ppt_k8s_parity.py \
  --kubeconfig /absolute/path/to/rl-training.kubeconfig \
  --namespace rl-training
```

`rubric_token` 用于 PPT runtime 调用 Reward adapter；`runtime_token` 用于 ORS/训练调用
PPT runtime。两者只是服务间 Bearer 鉴权，不是 AgentOS token，也不是模型 API key。
模型出口代理也只通过 `openreward-ppt-proxy` Secret 引用，不写进 Deployment 或 task。
默认 Reward 模型为当前 PPT 生产链路使用的 `ccop-4-6-agent`；可通过部署参数
`--model-name` 显式覆盖。

runtime Deployment 的 policy/render Pod 使用 deny-all 网络策略、只读 task inputs 和独立
workspace；runtime 仅保留创建/读取/删除 Pod 及读取日志所需的命名空间级 RBAC。`0610-deps`
缺少可执行 Node 时，runtime 通过 CFS 挂载的固定 Node binary 启动 `pptxgenjs`，仍不需要
Docker socket。

## 本地容器开发（不用于正式 rollout）

```bash
uv sync --extra dev --extra ppt-runtime

export ORS_PPT_POLICY_IMAGE=registry.example.com/acg-agi/ppt-rollout:0610-deps
export ORS_SLIDEFORGE_PIPELINE_ROOT=/absolute/path/to/SlideForgePipeline
export ORS_PPT_TASK_BUNDLE_ROOT=/absolute/shared/openreward-ppt-bundles
export ORS_PPT_ARTIFACT_ROOT=/absolute/shared/ppt-artifacts
export ORS_SLIDEFORGE_REWARD_WORK_ROOT=/absolute/shared/ppt-reward-work
# 由 secret manager 注入：ORS_PPT_RUNTIME_TOKEN / ORS_PPT_RUBRIC_TOKEN
# 由 secret manager 注入：MDRW_LLM_ENDPOINT / MDRW_LLM_API_KEY / MDRW_LLM_MODEL

docker compose -f deploy/openreward-ppt/compose.example.yaml up --build -d
export ORS_PPT_RUNTIME_URL=http://127.0.0.1:9103
```

不依赖真实 task bundle 的 Docker/LibreOffice control：

```bash
uv run --extra ppt-runtime python scripts/run_openreward_ppt_canary.py
```

它验证损坏 artifact 得到确定性 `0.0`，以及可编辑、可渲染 artifact 在没有 rubric 时保持
`reward=null, usable=false`。这只证明 sandbox 和 verifier，不替代下面的真实任务 canary。

先验证 8 条历史 evaluation 到 ORS 四维的确定性映射：

```bash
uv run python scripts/check_slideforge_reward_mapping.py
```

再生成与新 catalog 对齐的 selector：

```bash
uv run python scripts/export_openreward_slime_dataset.py \
  --catalog config/openreward-ppt-canary.json \
  --environment ppt \
  --split train \
  --mode interactive \
  --output data/example-ppt-bundles/slime_selector.jsonl
```

真实在线 control 仍需依次验证：损坏/空 PPT 得到可用的 `0.0`；历史合格 PPT 经实际
`POST /v1/evaluate` 得到非空 reward；最后再运行一条 policy-driven ORS episode。历史 HTTP
parity 命令如下，token 仍只从 `ORS_PPT_RUBRIC_TOKEN` 读取：

```bash
uv run python scripts/run_slideforge_reward_http_parity.py \
  --artifact-root "$ORS_PPT_ARTIFACT_ROOT"
```
