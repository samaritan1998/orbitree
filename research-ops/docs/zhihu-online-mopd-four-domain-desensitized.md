# 四类智能体环境，如何共用一套在线蒸馏训练系统

> 本文基于一次真实工程实践整理。平台名称、模型版本、数据规模、服务地址和运行参数均已泛化，不对应任何可直接访问的内部系统。

在线训练智能体时，最容易被低估的部分通常不是优化器，而是环境。

语言模型只负责生成下一步动作。一个智能体能否完成任务，还取决于工具是否真实执行、状态是否正确变化、观察结果能否返回，以及整段交互能否被还原成可训练的 token 序列。

当训练同时覆盖软件修复、演示文稿编辑、多工具操作和应用生成时，这个问题会更明显：四类任务的运行方式完全不同，却要进入同一个训练器。

本文介绍一种工程实现：用统一环境协议连接多类真实任务，再用 Multi-Teacher On-Policy Distillation（MOPD）训练一个共享的 student 模型。MOPD 的公开定义和实验结果见论文 [MOPD: Multi-Teacher On-Policy Distillation for Capability Integration in LLM Post-Training](https://arxiv.org/abs/2606.30406)。

## 先说结论

这套系统的关键不是「把四份轨迹转成同一种 JSON」，而是统一以下边界：

1. 任务如何创建独立会话。
2. 模型如何读取 prompt 和工具定义。
3. 动作如何进入真实环境并产生 observation。
4. episode 何时结束，失败如何表示。
5. student 生成 token 时的 behavior logprob 如何保存。
6. teacher 如何对同一串 token 评分。
7. optimizer 更新后，下一批 rollout 如何使用新权重。

只统一离线文件格式，不足以构成在线训练。真正的在线 MOPD 必须让「环境执行、token 记录、teacher 评分、参数更新、权重刷新」在一个持续运行的训练任务中循环发生。

## MOPD 到底训练什么

MOPD 的核心是让多个领域 teacher 指导同一个 student，并让训练数据来自 student 自身的 rollout：

- student 在真实环境中生成动作；
- 系统记录 student 当时使用的 token 和 behavior logprob；
- 冻结的 teacher 不重新生成答案，而是给 student 已生成的同一串 token 计算 teacher logprob；
- actor 用当前参数重新计算 train logprob，并根据蒸馏目标更新参数；
- 更新后的权重直接刷新到 rollout 服务，下一批数据由新 student 生成。

对第 `t` 个 response token，可以先看一个直观量：

```text
gap_t = log p_teacher(y_t | context_t)
      - log p_student(y_t | context_t)
```

训练目标会结合 loss mask、behavior logprob 和当前 train logprob，推动 student 缩小与 teacher 的 token 级差距。不同实现对重要性修正、裁剪和归一化的处理可能不同，但输入契约基本一致。

这里有一个容易混淆的点：**teacher 不需要再跑一条轨迹。**

如果 teacher 自己操作环境，teacher 和 student 很快会进入不同状态。此时两条轨迹的工具调用、观察结果和 token 边界都无法严格对应。让 teacher 只评分 student 的 token，可以直接消除这个问题。

## 为什么仍然需要真实环境

纯 MOPD 可以不把标量 reward 放进 loss，但不能省略环境。

环境至少决定四件事：

- 当前动作是否合法；
- 动作执行后状态如何变化；
- 下一轮模型能看到什么 observation；
- episode 在什么条件下结束。

没有这些状态变化，所谓的「在线轨迹」只是模型对静态文本的续写。

环境 reward 仍然有价值。它可以用于终止判断、样本可用性检查、旁路评测和错误分析。只是纯 MOPD 中，优化方向来自 teacher 与 student 的 token 概率差，而不是环境给出的单一分数。

如果后续要加入 RL，可以把 reward 作为另一种训练信号，但应使用独立配置明确区分：

```text
pure MOPD: teacher logprob -> distillation loss
reward RL: environment reward -> policy objective
hybrid:     两类信号显式组合
```

不要在同一次实验中隐式切换目标。

## 四类环境并不是同一种东西

四类任务可以共享协议，但不能共享一套具体实现。

| 环境 | 典型输入 | 主要动作 | 终态输出 |
|---|---|---|---|
| 应用生成 | 自然语言需求、应用类型、非敏感元数据 | 多轮规划、工具调用、代码或页面生成 | 应用产物、状态、完整模型调用记录 |
| 软件修复 | 仓库、基线提交、问题描述、测试定义 | 查看文件、编辑代码、运行命令和测试 | patch、测试摘要、验证结果 |
| 演示文稿 | 源文稿、素材、编辑要求、评价规则 | 读取结构、修改页面、渲染预览、提交文件 | 可编辑文稿、渲染结果、结构检查 |
| 多工具操作 | 任务描述、工具清单、数据库或工作区初始状态 | 调用 API、MCP 工具、数据库和文件系统 | 最终状态、工具记录、确定性检查结果 |

### 应用生成环境

应用生成通常运行在托管平台中。训练器不会直接控制它的每一步，而是提交一个异步任务，等待平台内部的智能体多次调用 policy 服务。

这类环境的难点是归因：一次应用生成可能包含多次模型调用，每次调用都有独立的 prompt token、response token 和 behavior logprob。最终导出的轨迹还可能经过字段补全或结构化处理。

因此，policy gateway 必须在请求发生时记录：

```text
episode_id
policy_call_id
prompt_token_ids
response_token_ids
behavior_logprobs
weight_version
request_status
```

训练数据不能从页面展示文本重新 tokenize，也不能只依赖请求全文哈希做关联。稳定的 episode ID 和 policy call ID 才是主键。

### 软件修复环境

软件修复任务需要可重复的仓库状态。一个完整 task spec 至少包括：

- 仓库或代码快照；
- 基线提交；
- 问题描述；
- 构建和测试入口；
- 隔离运行环境；
- verifier 所需但不能泄漏给模型的信息。

policy workspace 与 verifier workspace 应相互隔离。模型可以运行公开测试，但不能读取隐藏验证逻辑。episode 结束后保存 patch 和测试摘要，再清理临时容器或工作区。

### 演示文稿环境

演示文稿任务不是单纯生成一段 XML。环境需要同时维护：

- 源文稿和引用素材；
- 页面结构与可编辑对象；
- 渲染后的视觉结果；
- 内容、结构、视觉和可编辑性检查；
- 最终可下载的文稿文件。

模型通常需要在「修改文稿」和「渲染预览」之间往返。渲染图像可能进入下一轮上下文，因此环境协议应支持文本和图像 observation。

### 多工具环境

多工具任务的核心是可恢复的初始状态。除了工具 schema，还要固定数据库种子、文件系统、外部服务模拟和 evaluator。

每个 episode 应拥有独立 session。共享数据库或工作目录会让并发任务互相污染，也会让同一任务无法稳定重放。

## 用一个小协议统一四类环境

训练器不应理解每个环境的内部细节。它只需要一组稳定操作：

```text
create_session()           -> session_id
create_task(session, spec) -> environment_id
get_prompt(environment_id) -> prompt blocks
get_tools(environment_id)  -> tool schemas
call(environment_id, action)
                           -> observation, finished, metadata
delete_session(session_id)
```

这些名称只是示意，不代表某个固定 API。真正重要的是语义。

统一的 action 结果可以采用类似结构：

```json
{
  "blocks": [
    {"type": "text", "text": "environment observation"}
  ],
  "finished": false,
  "reward": null,
  "metadata": {}
}
```

`blocks` 可以包含文本、图像或文件引用。`metadata` 只保存训练和审计需要的非敏感信息，例如终止原因、产物摘要和验证状态。

协议需要明确以下失败语义：

- 动作格式错误；
- 工具执行失败；
- 环境不可用；
- episode 超过轮数限制；
- 远程异步任务超时；
- 产物已生成但 verifier 失败；
- 会话清理失败。

如果只返回一个 `success=false`，训练器无法判断样本应重试、丢弃还是保留已有 token。

## 在线任务数据与离线轨迹是两种数据

在线训练的输入不是历史回答，而是可以恢复环境的 task spec。

可以把数据分成三层：

```text
Task
  可执行任务定义，用于创建环境

Episode
  student 与环境的一次完整多轮交互

Sample
  送入优化器的 token 级训练单元
```

一个 episode 不一定只产生一个 Sample。

例如，托管应用生成平台在一个 episode 中调用 policy 五次。每次调用都有独立 response token 和 behavior logprob，可以转成五个 sibling Samples。它们共享 episode 的 `group_id`，但保留各自的 policy call ID。

一个可训练 Sample 至少应包含：

```text
tokens
response boundary
loss mask
behavior logprobs
teacher logprobs
policy weight version
domain
episode ID
sample ID
group ID
terminal metadata
```

历史轨迹仍可用于回归测试、格式验证和离线分析，但不能冒充在线任务。缺少仓库、素材、数据库种子或工具状态的轨迹，无法恢复真实环境。

## 三类 logprob 各自负责什么

在线策略训练中，经常同时出现三类 logprob。

### Behavior logprob

这是生成 token 时，rollout policy 给出的概率。

它必须与 token IDs 一起由真正提供响应的模型服务记录。训练后再用文本重算，会遇到模板变化、tokenizer 差异和权重版本不一致。

### Teacher logprob

这是冻结 teacher 对同一串 response token 的概率。

teacher 接收完整 token sequence，只评分，不采样，也不执行环境。teacher 请求失败时，应把 Sample 标记为不可训练，而不是填入一组 `0`。

### Train logprob

这是 actor 在反向传播前，用当前参数重新计算的概率。

它可能与 behavior logprob 不同，因为参数可能已经更新，或 rollout 与训练之间存在延迟。MOPD 目标需要明确区分三者，不能用一个笼统的 `logprobs` 字段代替。

## 一个 batch 如何完成在线训练

整个过程应运行在一个长期训练任务中：

```text
版本化 task store
        |
        v
按自然分布采样任务
        |
        v
student 在真实环境中 rollout
        |
        +--> token IDs + behavior logprob + weight version
        |
        v
teacher 对相同 token 打分
        |
        v
Sample 校验与补样
        |
        v
actor 计算 loss、backward、optimizer step
        |
        v
原进程刷新 rollout 权重
        |
        v
下一批使用新 weight version
```

每个 batch 的执行顺序如下：

1. DataSource 从任务池读取一批 task references。
2. Rollout Manager 按 domain 创建环境 session。
3. student 根据 prompt、tools 和历史 observation 生成动作。
4. 环境执行动作并返回下一轮 observation。
5. adapter 保存 token、mask、behavior logprob 和终态元数据。
6. teacher router 按 domain 选择冻结 teacher，并对相同 token 评分。
7. validator 丢弃缺少关联 ID、logprob 非有限或 token 数量不匹配的 Sample。
8. 如果可训练 Sample 不足，DataSource 继续补样。
9. actor 完成一次 optimizer update。
10. actor 将新权重刷新到 rollout engine。
11. 系统保存 checkpoint 和数据游标，然后处理下一批。

不要为每个 optimizer step 启动一个新的集群任务。这样会反复初始化模型、通信组和服务，训练时间会被启动开销吞掉，也无法验证原进程更新权重。

## 远程跑批平台必须显式接收 batch

托管应用环境还有一个特殊问题：训练器的一批数据，不应在平台页面上变成几十个独立跑批任务。

正确做法是先确定 optimizer batch，再为其中属于该环境的任务增加批次信封：

```text
optimizer_batch_id
expected_episode_count
episode_index
episode_id
```

环境 adapter 等齐同一 `optimizer_batch_id` 的成员后，只提交一次远程 batch。batch 内每条 record 保留独立 `episode_id`。

这解决了两个问题：

1. 平台任务数量与 optimizer batch 对应，便于监控和停止。
2. 每条 episode 仍可独立关联 trajectory、policy call 和 logprob。

不要依赖一个很短的时间窗口猜测哪些请求属于同一批。高负载下，请求到达时间会抖动，结果可能是残缺 batch 或大量单条任务。

## 超时不是一个数字

多轮智能体系统通常有多层超时：

```text
单次模型调用超时
环境等待新消息超时
单轮动作超时
整个 episode 超时
远程 batch 超时
训练任务超时
```

这些值必须满足合理的包含关系。

例如，单次模型调用允许的最长时间大于消息流空闲超时，那么正常的长生成也会被错误中止。此时看到的现象是「环境长时间没有消息」，真正原因却是超时层级配置矛盾。

诊断这类问题时，应同时比较：

- 模型请求开始和结束时间；
- 首 token 与末 token 时间；
- 环境最后一条事件时间；
- policy HTTP 状态；
- episode 终止时间。

只有环境确实没有产生预期状态变化时，才能把问题归因到环境。

## 自动重试可能污染训练样本

远程平台常有 record 级自动重试。对普通离线任务，这很方便；对 token 级在线训练，它可能破坏关联关系。

如果一次失败 attempt 和后续成功 attempt 复用同一个 episode ID，导出的 trajectory 可能包含两组 policy calls。此时 behavior logprob 无法确定对应哪一次环境状态。

更稳妥的策略是：

- 禁用环境内部的静默自动重试；
- 失败 episode 返回明确状态；
- 由训练器用新的 occurrence ID 补样；
- 每次 attempt 保留独立、可审计的身份。

## weight version 是在线训练的必要证据

checkpoint 文件出现，不代表下一批 rollout 已经使用新模型。

每个 policy response 都应携带非默认的 `weight_version`。一次完整验证至少需要看到：

```text
batch N:     student version V0
optimizer:   parameters updated
refresh:     rollout engine accepts V1
batch N + 1: student version V1
teacher:     fingerprint unchanged
```

如果第二批仍然报告 `V0`，说明训练与 rollout 服务之间的权重更新没有生效。此时即使 optimizer 正常运行，系统也不是真正的在线训练。

## 推荐的实现顺序

不要一开始就混合所有环境和大 batch。一个更容易定位问题的顺序是：

### 阶段一：验证统一协议

每个环境各选一个最短任务，完成：

```text
create -> prompt/tools -> call -> finished -> delete
```

验收重点是状态变化和资源清理，不是模型效果。

### 阶段二：验证单条 Sample

选择一个模型能稳定完成的任务，检查：

- token IDs 与 response boundary 一致；
- loss mask 至少包含一个训练 token；
- behavior logprob 数量正确且全部有限；
- teacher logprob 数量正确且全部有限；
- weight version 不为默认值；
- episode、sample 和 group ID 可追踪。

### 阶段三：完成一次 optimizer update

用在线生成的 Sample 运行一个最小训练 batch。确认有限 loss、有限 gradient 和有效 checkpoint。

### 阶段四：验证原进程权重刷新

不要退出训练任务。让 actor 刷新 rollout engine，再生成第二批 Sample，确认 weight version 改变。

### 阶段五：扩大单域小批量验证

每个环境分别扩大到小批量，观察成功率、长尾耗时、资源清理和 Sample 通过率。

### 阶段六：按真实数据分布混合训练

任务池按 epoch 洗牌，自然产生混合 batch。除非实验明确研究域权重，否则不要为了表面整齐而强制每个 batch 四域均分。

## 最容易踩的十个坑

### 1. 把离线轨迹当成在线任务

轨迹缺少环境初始状态，无法创建真实 episode。

### 2. 让 teacher 自己再跑一遍环境

两条轨迹很快分叉，token 级监督无法严格对应。

### 3. 从最终文本重新 tokenize

模板、tokenizer 或结构化工具格式变化都会让 behavior logprob 失去对应关系。

### 4. teacher 失败时填零

这会把服务故障伪装成有效监督。正确处理是拒绝 Sample 并记录原因。

### 5. 用短时间窗口聚合远程任务

高负载下会产生残缺 batch 或大量单条任务。批次边界应由显式 ID 和期望数量定义。

### 6. 让平台自动重试复用 episode ID

多个 attempt 的 policy calls 会混在一起，无法可靠关联 token。

### 7. 每一步启动一个新训练任务

初始化时间远大于 optimizer 时间，也无法证明权重在原进程中刷新。

### 8. 只看平台状态

控制面显示「运行中」不等于训练有进展。还要检查 Pod、日志时间、rollout 计数、iteration 和 checkpoint。

### 9. 只看 checkpoint，不看 weight version

新 checkpoint 可能从未被 rollout engine 使用。

### 10. 因为 reward 不进 loss 就跳过环境

没有 observation 和状态变化，得到的是离线语言建模数据，不是在线智能体数据。

## 一份可执行的验收清单

### 数据

- task store 有版本和哈希。
- 训练集与评测集严格隔离。
- task spec 足以恢复环境。
- 隐藏 verifier 不进入模型 prompt。

### 环境

- 四类环境均能创建、执行和清理 session。
- 并发 episode 不共享可变工作区。
- 终止状态和失败原因明确。
- 长任务的超时层级一致。

### 模型

- student checkpoint lineage 明确。
- teacher checkpoint 和 route fingerprint 可验证。
- teacher 只评分 student token，不另生成轨迹。
- 每次 policy response 都记录 weight version。

### Sample

- token、mask 和 response boundary 数量一致。
- behavior、teacher 和 train logprob 语义分离。
- 所有参与计算的 logprob 都是有限值。
- episode、sample、group 和 policy call ID 可关联。

### 训练

- 一个长期任务包含多个 optimizer update。
- 首次 update 产生有限 loss 和 gradient。
- checkpoint 同时保存模型、optimizer 和数据游标。
- 下一批 rollout 使用变化后的 student weight version。
- teacher fingerprint 在训练期间保持不变。

## 最后

多环境在线 MOPD 的难点，不在于把四种数据塞进同一个 batch，而在于保持三类一致性：

1. **环境一致性**：动作、观察和终止状态属于同一个 episode。
2. **token 一致性**：token IDs、mask、behavior logprob 和 teacher logprob 严格对应。
3. **版本一致性**：生成数据的 student、参与训练的 actor 和下一批使用的权重版本都有明确记录。

统一协议解决环境差异，稳定 ID 解决跨系统关联，长期训练任务解决权重更新。三者同时成立，才是一套可以持续运行、可以定位问题、也可以复现实验的在线蒸馏系统。
