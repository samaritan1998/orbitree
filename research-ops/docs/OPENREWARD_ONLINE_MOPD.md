# OpenReward 在线 MOPD 闭环

统一 ORS 负责环境状态、工具调用和 episode 终止；task reward 是否必需由训练目标决定。
严格 MOPD 要求 policy gateway 返回行为 token/logprob，并由 teacher 对同一完整 token
序列返回 teacher logprob。纯 MOPD 不使用环境 task reward：Slime 通用 batch schema 中的
`Sample.reward` 固定为中性的 `0.0`，梯度只来自 teacher/student token logprob 的 OPD KL。
Goblin、SWE verifier、PPT rubric 和 ToolUse evaluator 可异步用于评估，但不能进入纯 MOPD
loss。

## 当前四域 teacher 路由

当前实验是一个共享 student、按 `domain` 选择冻结 teacher 的四域 MOPD：

| domain | 冻结 teacher |
|---|---|
| `miaoda` | `demo-teacher-base/iter_0000023_hf` |
| `swe` | `demo-teacher-base/iter_0000023_hf` |
| `ppt` | 由此前 2,383 条候选 PPT 轨迹训练的 PPT 专家 checkpoint |
| `tooluse` | `demo-teacher-base/iter_0000023_hf` |

student 也从 `demo-teacher-base/iter_0000023_hf` 初始化，但训练必须写入新的
checkpoint，绝不能更新任一 frozen teacher。完整、无凭据的 checkpoint 映射见
[`config/openreward-four-domain-teacher-routing.yaml`](../config/openreward-four-domain-teacher-routing.yaml)。
在线 serving 可以使用各 checkpoint 对应的 visual/HF 产物；路由语义和冻结 lineage 不变。

四域 launcher 使用两层文件：版本化 routing YAML 只保存 checkpoint 和 receipt 环境变量名；
实际 serving job 写出 `endpoint.json` 后，将其路径注入下列两个运行时引用：

```text
OPENREWARD_XMLSFT_I23_TEACHER_ENDPOINT_FILE
OPENREWARD_PPT2383_TEACHER_ENDPOINT_FILE
```

控制器的 cycle manifest 增加以下块即可生成 route 文件，并为每个带 `domain` 的 rollout
phase 注入 `teacher_routes_file`。原有的 `teacher_endpoint_file` 会被移除，避免旧单 teacher
receipt 覆盖新路由。

```yaml
teacher_routing:
  config: ../config/openreward-four-domain-teacher-routing.yaml
  output: ../data/four-domain-cycle/teacher-routes.json
  check_health: false

phases:
  - name: swe-rollout
    domain: swe
    config: ../deploy/ltp/openreward-swe-policy-0724/config-train-z-r18-poststep2-canary.yaml
    config_output: ../data/four-domain-cycle/generated/swe.yaml
    run: ../deploy/ltp/openreward-swe-policy-0724/run.sh
    ready:
      path: ../data/four-domain-cycle/swe.json
```

`check_health: false` 表示 controller 只验证 receipt 和 checkpoint lineage；最终 `/health`
检查由每个 `cluster-d` rollout pod 在自身网络命名空间内完成。设为 `true` 可在提交前额外检查
controller 到 serving endpoint 的连通性。可以单独执行以下命令做相同的 receipt 物化或校验，
命令输出不会打印 endpoint URL：

```bash
uv run python scripts/resolve_openreward_teacher_routes.py \
  --routing config/openreward-four-domain-teacher-routing.yaml \
  --output data/four-domain-cycle/teacher-routes.json
```

对应的两份 `cluster-d` serving bundle 已预置，均复用
`deploy/ltp/openreward-mopd-teacher-0723/run.sh`：

- `config-train-z-i23-base-0731.yaml`：Miaoda/SWE/ToolUse 共用的 i23 teacher；
- `config-train-z-ppt2383-0731.yaml`：PPT-2383 专用 teacher。

两个 job 各自写出 endpoint receipt 后，才设置上述两个 `*_ENDPOINT_FILE` 运行时引用并启动
four-domain cycle。旧 `online-mopd-smoke` teacher receipt 的 checkpoint 不匹配，会被 resolver
明确拒绝。

只有 reward-backed RL 或 RL + OPD 混合目标才要求 terminal task reward。两种模式不得在
同一实验中隐式切换：纯 MOPD 样本必须同时记录
`openreward_training_objective=pure_opd`、`openreward_task_reward_used=false` 和
`openreward_reward_source=pure_opd_neutral`。

解耦闭环使用五个阶段：

```text
fresh ORS rollout
  -> Slime .pt（token / mask / behavior logprob / teacher logprob / neutral reward=0）
  -> one optimizer step
  -> torch_dist checkpoint -> HF conversion
  -> versioned SGLang gateway
  -> post-update ORS rollout -> transition verification
```

`.pt` 是新鲜在线 episode 的耐久阶段交接，不是历史训练集。控制器在收到上一阶段 receipt
后立即启动下一阶段，因此环境服务、训练框架和 serving 可以独立扩缩容或失败重试。严格的
同进程路径则直接把刚生成的 `Sample` 交给 optimizer，不经过 `.pt` replay；两条路径必须使用
相同的 token、mask、behavior logprob、teacher logprob 和纯 MOPD 元数据校验。

Miaoda 纯 MOPD 只等待 Datawheel/AgentOS rollout 形成明确 episode boundary；它不提交或
轮询 Goblin。Goblin reward 可在旁路按 batch/app ID 补算，用于 benchmark 曲线和失败归因。

## 已验证证据

2026-07-24 的 ToolUse r25 reward-backed 闭环已由控制器完整验证。它证明了分阶段
rollout/optimizer/conversion/gateway/transition 的工程链路，但其中 reward=1 是历史控制实验，
不能作为纯 MOPD 必须使用 reward 的依据：

- 更新前两条 episode 的 reward 均为 `1`，behavior/teacher logprob 分别为
  `5594/5594` 和 `5624/5624`，全部有限；
- optimizer 产出 `iter_0000001`，随后完成 torch-dist 到 HF 转换；
- gateway receipt 验证版本从 `default` 刷新为 `online-mopd-r25-iter1`；
- 更新后一条真实 episode 的 reward 为 `1`，5754 个 behavior logprob 和 5754 个 teacher
  logprob 均有限；16 次 policy 调用全部报告 `online-mopd-r25-iter1`；
- 统一校验收据：
  `data/example-mopd-run/online_mopd_transition_r25_verified.json`；
- 可断点续跑的完整周期状态：
  `data/example-mopd-run/mopd_cycle_r25_state.json`，其中 `verified=true`。

r8 收据仍保留为更早的独立复现实例：
`data/example-mopd-run/online_mopd_transition_r8_verified.json`。

## 单周期控制器

控制器固定向 `cluster-d` 提交任务，逐阶段执行严格 LTP bundle 校验，保存 job ID 和状态，
并以 durable receipt 断点续跑。它不读取或写入任何 token；鉴权仍由各 LTP `run.sh` 从运行时
引用取得。

```bash
uv run python scripts/run_openreward_mopd_cycle.py \
  --manifest config/openreward-mopd-cycle-r25.yaml
```

manifest 的阶段可使用静态 `config`，也可用 `config_template + env_from_json`。后者用于从
gateway receipt 注入实际可达的 `policy_url`，然后再做严格 preflight。Gateway 是常驻任务，
其阶段在 receipt 验证后即可继续；控制器不会停止 serving job。

若某个 live 融合探针会在 episode 已写入 durable receipt 后因已知的 actor backward 问题退出，
该 phase 必须显式声明 `allow_ready_after_failure: true`；控制器只在 ready receipt 完整时接受它，
普通 production phase 仍对任何 LTP failure fail-closed。这保证了“在线 rollout 产物交接”和
“训练进程是否成功”不会被一个隐式的失败状态混淆。

## 同进程融合路径

Slime 的 `rollout_manager.generate -> actor.async_train` 同进程路径目前仍是可选优化，不是稳定
闭环的前提。多次真实 live batch 在第一轮 backward 出现 rank-local NaN，而同一份序列化
样本 replay 的 loss/gradient 有限。`forge_load` 进一步证明，即使输入完全相同，只要保留
SGLang、placement group 和权重更新生命周期仍会复现 NaN；因此问题位于融合运行时，而不在
reward、teacher logprob 或 episode 数值。后续诊断继续隔离 global rollout dataset 和 actor
启动前的 GPU keeper 状态。

在融合路径修复前，生产闭环应使用上述 receipt 驱动的解耦阶段；不得把 NaN batch 当成零
reward，也不得伪造 teacher logprob。
