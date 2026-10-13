# Research Ops 使用手册

默认访问地址：<http://127.0.0.1:8020/>。远程部署的地址和登录方式由部署者配置；`https://research.example.com/` 仅为示例。

浏览器和通知链接使用上述域名；服务器本地的 `rops` 控制请求及健康检查仍使用 `http://127.0.0.1:8020`，无需经过登录网关。已有会话路径保持不变，旧链接只需将 `http://198.51.100.232:8020` 替换为新域名。

这套系统不替代 Codex。本地终端仍是功能完整、优先级最高的 Codex 入口；Web 负责管理工作树、状态、每轮最终结果、Job、Handoff 和 Memory，也可以在 Results 中提交一轮轻量对话。

## 30 秒版本

第一次只照下面做：

1. 打开 Today。
2. 还没想清楚时直接点击 `Create scratch work`，不用填写标题或目标。
3. 在右侧点击 `Start locally`，复制命令。
4. 在服务器终端粘贴命令，在原生 Codex 窗口中工作。
5. 每轮结束后回到 Web 的 `Results` 查看最终总结。
6. 想并行尝试另一种方案时点击 `Fork`。
7. 退出终端后，在 Web 点击 `End this round` 保存 Handoff。

其他功能可以等实际遇到 Memory、训练 Job、结果合并时再查对应章节。

## 先记住四个概念

| 概念 | 你可以把它理解成 | 什么时候创建 |
| --- | --- | --- |
| Program | 长期方向 | 系统已经配置好，通常不用新建 |
| WorkItem | 一段可以持续、分叉和回看的工作 | 有一个想法、问题或任务时，尚未想清楚也可以 |
| Branch | 同一任务下的一条方案或假设 | 想尝试另一种方法，但不想打断当前工作时 |
| Session | 一次受管的本地 Codex 进程 | 在终端执行 `rops open` 时创建 |

它们的关系是：

```text
Program
  └── WorkItem
        ├── Main Branch ── Session 1 ── Handoff ── Session 2
        ├── Experiment A Branch ── Session
        └── Experiment B Branch ── Session
```

最重要的原则：

- WorkItem 是长期记录，Codex 窗口不是。
- 先开始再定义是正常流程；Codex 会在意图清晰后自动回填标题和目标。
- Branch 表示不同探索路径，不表示一次新的聊天。
- Session 结束后，可以在原 Branch 上再次 `Continue locally`，不需要重新建任务。
- 同一个目标下突然想到另一种方案，使用 `Fork`；目标本身变了，使用 `New work`。

## 五个方向怎么选

| Program | 放什么工作 |
| --- | --- |
| HARN / Harness / Office | PPT 轨迹、PresentBench、Office Agent、秒哒泛化 Harness |
| COST / Model Cost | GLM 低精度训练、FP8/FP4、专家裁剪、训练和推理降本 |
| ADAPT / Model Adaptation | 新开源模型结构适配、权重转换、训练、评估和上线 |
| COMMON / Shared Platform | 三个方向都会复用的工具、数据、Benchmark、基础设施 |
| FREE / Free Work | 暂时没有明确归属的想法、临时验证、待整理工作 |

不确定放哪里时，点击 `Create scratch work`，系统会立即在 `FREE` 创建一个未定义工作项。误点后可在页面右下角点击 `Undo`；这只会从系统视图隐藏工作项，不会删除它的目录或文件。

## 第一次使用：从零开始跑一个任务

### 第一步：直接开始，或创建已定义的 WorkItem

还只有一个模糊想法时：

1. 打开 Today。
2. 点击 `Create scratch work`。这个操作会立即创建任务，创建后右下角会出现 `Undo`。
3. 系统立即创建一个 `FREE` 工作项并打开 Main Branch，不要求标题、目标或完成条件。
4. 在 Codex 中自然讨论和执行；当意图、边界和预期结果足够清晰时，Codex 会调用受管命令回填标题和目标，Web 会自动刷新。

已经想清楚时，可以点击左下角 `New work`，直接选择方向和补充以下字段。

| 字段 | 怎么填 |
| --- | --- |
| Program | 选择任务所属方向 |
| Title | 必填。一句话说明要交付什么；尚未想清楚时不要打开表单，直接使用 Create scratch work |
| Objective | 可选。描述要解决的问题、边界和预期结果 |
| Parent work | 如果它是另一个任务的子任务，则选择父任务；否则选 Program root |
| Acceptance | 写一个最关键、可判断的完成条件 |
| Mode | 一般选择 Writable branch |
| Repository | 有代码仓库就选已注册仓库；没有就用 Scratch workspace |

示例：

```text
Program: HARN
Title: PresentBench PPT editing trajectory verifier
Objective: 为可编辑 PPT 多轮轨迹实现确定性校验器，并分析失败轨迹中的布局错误。
Acceptance: 现有 50 条 fixture 全部通过，且失败结果包含可定位的 evidence。
Mode: Writable branch
Repository: Scratch workspace
```

创建成功后，系统会直接打开该 WorkItem 的 Main Branch。

如果 Title 或 Objective 留空，工作项会暂时显示为 `未命名工作`。这不是错误，也不会阻止启动 Codex。

### 第二步：在本地终端启动 Codex

右侧 Branch 面板会显示一个主状态和一个主操作。

1. 状态为 `Ready`、`Expired` 或 `Error` 时，点击 `Start locally` 或 `Continue locally`。
2. Web 会复制一条 `.venv/bin/rops open BRANCH_ID` 命令。
3. 在同一服务器的终端粘贴并执行。终端启动后，Web 状态才会变为 `Running`。
4. 在原生 Codex TUI 中自然对话、审批命令和执行任务。

例如：

```text
先检查当前 Brief 和可用 skill，给出这个 verifier 的实现计划，然后开始实现。
```

`rops open` 会自动进入该 Branch 的独立目录，保持 Session heartbeat，并在每轮完成后把 prompt、final answer 和耗时同步到 Results。Results 底部也可以提交一轮 Web 对话；同一时间只允许一轮，完成、失败或被打断后才会重新开放输入。本地 CLI 不受这个限制，并且拥有更高优先级：本地新一轮开始时，会中断正在执行的 Web 轮；本地一轮未完成时，Web 发送按钮保持禁用。

### 第三步：结束本次工作

工作告一段落时，先正常退出 Codex TUI。Web 状态会离开 `Running`，随后点击 `End this round`。

Handoff 至少填写：

- Outcome：这次执行是继续、完成、阻塞还是等待外部任务。
- Conclusion：已经得到什么结果。
- Next action：下一次从哪里继续。

常用 Outcome：

| Outcome | 使用场景 |
| --- | --- |
| Continue later | 今天先停，下次在同一 Branch 继续 |
| Ready for review | 当前方案已经完成，可以检查结果 |
| Waiting external | 已提交训练、评测或部署任务，等待远端结果 |
| Blocked | 缺少输入、权限、资源或关键决策 |

下次打开这个 Branch 后点击 `Continue locally`，新 Session 会带上上一次 Handoff。

如果这轮工作形成了可重复执行、以后还会用到的操作方法，可以在保存前勾选
`Extract reusable skill for review`。Handoff 会立即保存，AI 提炼在后台运行，不会卡住结束流程。
提炼结果进入左侧 `Skills > Review inbox`，不会直接改动已有 skill。

## 什么时候用 Fork

假设 Main Branch 正在实现规则校验器，你突然想试一个视觉模型校验方案。

不要停止 Main，也不要新建完全独立的 WorkItem。点击 Program 页右上角 `Fork`：

```text
Name: vision-verifier
Objective: 使用视觉模型校验布局重叠，与规则校验器做配对比较。
Mode: Writable branch
After fork: Continue in the child branch
```

Fork 后：

- 默认等待父 Branch 当前轮结束；只有用户明确点击 Fork 后，Web 才会进入已创建的子 Branch，后续消息发给子 Branch。
- 创建 fork 不会自动启动子 Branch 的 Codex 轮次。需要执行时，由用户在子 Branch 页面点击 `Start child continuation`；页面会显示 `queued / running / completed / failed`，失败可点击 `Retry child`。Agent 不能替用户创建 fork，也不能替用户点击这个启动动作。
- 父 Branch 标记为 `Continued`，仍保留完整历史；需要时可以显式 `Reopen parent`。
- 子 Branch 获得独立目录或 Git worktree。
- 已存在的 Codex thread 上下文会被继承。
- 若长 thread 无法直接 fork，系统自动改用“新 thread + 冻结 Handoff”，不会留下空 Branch。
- 父 Branch 当时可见的 accepted Memory 会被冻结。
- 父子 Branch 后续终端 Session 和文件修改互不干扰。

不再需要显示某个子 Branch 时，可以在详情顶部点击 `Hide`。这是可恢复的软隐藏：不会删除
目录、Git 分支、Codex Session、对话记录或真实父子关系。若隐藏的是一个空中间节点，它的
可见后代会在图中连接到最近的可见祖先。Program 顶部的 `Hidden N` 可以临时显示隐藏节点，
进入节点后点击 `Restore` 即可恢复。主 Branch 和仍有活跃轮次的 Branch 不能隐藏。

如果要让两个方案同时推进，把 `After fork` 改成 `Keep this branch active`；CLI 对应
`rops fork ... --parallel --approved-by-user`。若页面显示 `Codex fork was not prepared`，点击 `Repair fork`
即可在保留原 Branch ID、目录和来源关系的情况下重新建立 thread。

适合 Fork 的情况：

- 比较两个算法或训练配置。
- 尝试不同修复方案。
- 新想法可能失败，但不想污染当前工作。
- 需要并行推进两个实验。

不适合 Fork 的情况：

- 只是继续昨天的工作：在同一 Branch 点击 `Continue locally`。
- 目标已经完全不同：创建 `New work`。
- 只是记录一个暂时不做的想法：在 `FREE` 创建 Capture only WorkItem。

## 状态和主操作

详情页分别展示 Work 和 Session 状态：

- `Work` 表示任务现在是否可执行、被阻塞、等待外部结果或已经完成。
- `Session` 表示最近一次本地 Codex 进程是否仍在运行。

因此 `Work Ready / Session Expired` 不矛盾：任务可以继续，只是上一次 Codex 窗口已经结束，
此时主操作是 `Continue locally`。

| Work 状态 | 含义 | 主操作 |
| --- | --- | --- |
| Ready | 任务当前可以执行 | Start locally 或 Continue locally |
| Running | 任务有受管终端正在执行 | 在本地 Codex TUI 工作 |
| Blocked | 当前无法继续 | Update，记录或解除阻塞 |
| Waiting external | 正在等待训练、评测、部署等远端任务 | 查看 Jobs，结果回来后 Update |
| Needs decision | 需要人工决策 | Update 状态后继续 |
| Review | 工作结果等待检查 | 查看 Result、Memory 和 Artifact |
| Done | WorkItem 已完成 | 通常无需操作 |
| Cancelled | WorkItem 已取消 | 通常无需操作 |

| Session 状态 | 含义 |
| --- | --- |
| Not started | 尚未启动过 Codex |
| Running | `rops open` 进程存活且租约有效 |
| Expired | 上一次进程已退出或租约过期，结果仍保留 |
| Completed | 上一次 Session 已正常结束并完成 Handoff |
| Error | 上一次 Session 启动或执行失败 |

浏览器是否打开不会改变 Session Running；`rops open` 子进程或已明确纳管的本地 Codex
进程存活并续租时才是 Running。

## Today 怎么看

Today 是每天进入系统后的第一个页面。

- Continue：还没有结束的 WorkItem。行内状态可能是 Ready 或 Running。
- Needs decision：需要你明确做决定的工作。
- Waiting external：已经提交到外部系统、正在等待的 Job。
- Ready next：已经定义好、可以开始但还没启动的工作。

点击整行即可打开对应 WorkItem。箭头只是图标，不是第二个按钮。

建议每天早上：

1. 先处理 Needs decision。
2. 检查 Waiting external 是否已经返回结果。
3. 从 Continue 中选择 1–3 个任务推进。
4. 不要因为 Session 显示 Expired 就新建任务，直接点击 Continue locally。

## Sessions：管理服务器上零散的 Codex 窗口

左侧 `Sessions` 会扫描当前用户的 `~/.codex/sessions` 和本机 Codex TUI 进程。它只读取
Thread ID、父 Thread ID、PID、TTY、工作目录和活动时间，不读取进程环境变量或 Codex
凭据。默认的 `Lineage` 视图会依据 Codex rollout 中持久化的 `forked_from_id` 自动还原
本地窗口的真实父子树；`List` 用于紧凑浏览和筛选。

Sessions 只展示你可以独立继续、Attach 或恢复的用户窗口。Codex 内部通过 subagent 工具
启动的执行线程会依据结构化 `source.subagent.thread_spawn` 元数据自动识别：它们仍保留在
底层注册表中用于排障，但不会进入页面、数量统计或 Lineage，因此不会把一次并行研究显示成
几十个窗口。

窗口卡片的主标题按以下优先级确定：

1. 手工修改的名称。
2. 已纳管 WorkItem 和 Branch 的名称。
3. 后台 AI 根据最近几条真实用户请求生成的短标题。
4. AI 尚未完成时的目录名和 Thread 前缀。

后台命名复用本机 Codex 登录态，以只读 `--ephemeral` 方式批量运行，不产生新的可恢复
Session，也不需要额外 API Key。点击 `Name windows` 可以重新为尚未手工命名的窗口生成
标题；卡片上的铅笔按钮用于手工改名，手工名称不会被 AI 覆盖。`Attach to Program` 会采用
这个窗口标题创建 WorkItem，不再生成 `Codex session · wide` 一类难以区分的名称。

这里有两棵彼此独立、但可以对齐的树：

```text
Codex Lineage                       Research Ops
root thread                         Program
  └── forked child thread    <->      └── WorkItem
        └── forked grandchild                 └── Branch
```

- Codex Lineage 是已有对话的事实关系，扫描时自动发现，不需要手工登记。
- Research Ops 树表示工作归属，由你通过 Attach 决定。
- Attach 不会修改 Codex Lineage；把窗口换到另一个方向后，原始父子关系仍然保留。
- 父 Thread 暂时没有扫描到时，子 Thread 会作为当前可见树的根展示；父项出现后会自动归位。

| 状态 | 含义 |
| --- | --- |
| Unassigned | 检测到仍在运行的 Codex，但尚未关联 Research Ops 工作 |
| Managed | 已经关联 WorkItem / Branch，最终结果会同步到 Results |
| Saved | 当前没有运行进程，但 Thread 仍可通过 `codex resume` 恢复 |
| Ignored | 仅从默认列表隐藏；进程、Thread 和文件均不受影响 |

常用操作：

- 复制图标：复制包含 Thread ID 和原工作目录的恢复命令。
- `Attach under WORK-ID`：当父 Thread 已纳管时，将这个 fork 窗口登记成同一 WorkItem 下、
  位于父 Branch 下面的子 Branch。继续采用子窗口原目录，不移动、不复制、不修改文件。
- Attach Branch：只显示工作目录完全一致的已有 Branch，确认后建立关联。
- Attach to Program：选择方向后自动创建一个轻量 WorkItem，直接采用原工作目录，不要求先填写标题或目标，也不会移动或修改文件。
- 隐藏图标：从默认列表隐藏；切换 Status 为 Ignored 可以恢复显示。

最常见的挂载流程：先扫描，在 `Lineage` 中找到父窗口和 fork 子窗口；先把父窗口 Attach
到一个方向或已有 Branch，子窗口随后会出现 `Attach under WORK-ID`。点击后，Program
图中会出现父 Branch 到子 Branch 的边，Sessions 中仍显示原始 Codex fork 树。若子窗口
实际上已经变成另一个目标，则改用 `Attach to Program`；它会进入新的 WorkItem，但
Sessions 仍如实显示它从哪个 Thread fork 而来。

多个 Codex 进程使用同一个目录时，系统会显示 `Multiple processes share this directory`，
不会猜测 PID 和 Thread 的对应关系。此时可以退出多余窗口后重新扫描，或在终端按 Thread ID
显式登记：

```bash
cd /srv/research-ops/app
.venv/bin/rops windows
.venv/bin/rops windows --all  # 包含没有运行进程的历史 Thread
.venv/bin/rops adopt THREAD_ID --program FREE
.venv/bin/rops adopt THREAD_ID --branch BRANCH_ID
```

`adopt --branch` 同样要求目录完全一致。Sessions 页面不提供停止进程操作，避免误关仍在工作的终端。

## Program Desk：每个方向的父窗口

每个方向页面顶部都有一个 `Desk`。它是该方向长期保留的父 Codex 窗口，不是普通任务，
因此不会出现在 Today、Graph、List 或任务数量中。

Desk 适合：

- 回顾该方向所有任务、Fork、Handoff 和最近一轮最终总结。
- 讨论优先级、拆分新任务和比较多条实验路径。
- 记录方向级决策、流程和已验证结论。
- 创建 WorkItem 后，让实际实现进入独立任务窗口。

Desk 不应该直接进入子任务目录修改代码。点击 Program 页的 `Desk`，再点击
`Start locally`；复制出的命令类似：

```bash
/path/to/rops desk HARN
```

Desk 中可以随时运行下面的只读命令刷新方向上下文：

```bash
/path/to/rops desk-context HARN
```

建议本地终端按方向保留三个父 Tab：`HARN Desk`、`COST Desk`、`ADAPT Desk`；
`FREE Desk` 按需打开。真正执行任务时再开启 `HARN-0001 primary` 或某个 Fork 的临时 Tab，
完成 Handoff 后关闭临时 Tab。

Desk 中提出的 Memory 使用 Program scope。只有审核为 accepted 后，才会进入该方向后续
子任务的 Brief。

## Q3 目录怎么组织

所有可见任务都使用以下逻辑目录结构：

```text
/srv/research-ops/workspaces/
├── HARN/
│   ├── _desk/
│   └── tasks/
│       └── HARN-0002/
│           ├── primary/
│           └── forks/data-diversity/
├── COST/
├── ADAPT/
├── COMMON/
└── FREE/
```

默认路径在 `primary`，只有真实 Fork 才进入 `forks/<name>`。新任务直接创建在这里；
已有任务启动时会自动补一个指向原 Branch 的符号链接。因此 Q3 中可以按方向统一浏览，
但系统不会搬动、复制或重命名原文件。

Branch 的 Brief 会同时展示：

- Workspace：Q3 中适合日常查找的逻辑路径。
- Original Branch location：旧任务真正的原始路径；新任务两者相同，因此不重复显示。

若 Q3 目标位置已经存在其他文件，系统不会覆盖它，而是继续使用原路径并在日志中报告冲突。
不要手工搬动原始 Branch；Codex Thread、历史 Result 和证据引用仍依赖其稳定位置。

## Graph 和 List 怎么选

Program 页面支持两种视图：

- Graph：适合查看父子任务、Fork 和复杂分支关系。
- List：适合只有几个节点时快速浏览和进入任务。

两种视图展示的是同一份数据，可以随时切换。选择 List 后 URL 会带上 `?view=list`，刷新、收藏或分享链接都会保留当前视图。

每个 WorkItem 内部都有一条默认主路径，用来保存独立目录、Codex thread、Session 和结果。
这条路径不会单独显示成 `Main` 节点；WorkItem 本身就是主路径入口。只有点击 `Fork`
创建出来的真实分支才会作为额外节点显示，因此通常看到的结构是：

```text
Program → WorkItem → Fork A → Fork A.1
                   └→ Fork B
```

## Jobs：训练和评测不需要一直挂着窗口

### 统一提交 CLI

试用阶段可以通过 `rops job` 提交计算任务。用户只选择硬件类别，不选择具体集群；集群名和
LTP 实际任务名只保存在控制面内部，不会出现在 CLI 状态中。

```bash
export ROPS_CONTROL_URL=http://127.0.0.1:8020
.venv/bin/rops job submit /绝对路径/bundle \
  --work COMMON-0007 --hardware b --kind inference
.venv/bin/rops job status LOGICAL_JOB_ID
.venv/bin/rops job list
```

bundle 目录至少包含 `config.yaml` 和 `run.sh`。正式训练还必须使用 `--receipt` 提供已经完成
通知的 preflight receipt；单独的训练脚本使用 `--train /绝对路径/train.py` 指定。CLI 会按
当前操作系统用户、Work ID、任务类别和 bundle 内容生成幂等键，同一条命令重复执行不会重复
创建逻辑任务。

用户可见状态只有 `accepted`、`verifying`、`queued`、`running`、`completed`、`failed` 和
`needs_review`。其中 `needs_review` 表示外部提交结果不确定，系统会停止自动重试，避免重复
创建真实任务。

服务默认只接收逻辑任务，不执行真实 LTP 提交。控制面管理员完成 LTP 身份和共享路径核验后，
再设置 `ROPS_MANAGED_JOB_SUBMIT_ENABLED=true`。当前试用版从本机操作系统账号读取提交人；正式
开放给多用户前，还需要在 API 层把访问凭据与用户身份绑定。

### 登记已有远端任务

提交 LTP、Datawheel、Reward、部署或其他远端任务后：

1. 打开 Branch 的 `Jobs` 标签。
2. 点击 `Register job`。
3. 填写 Provider、Remote job ID、Status 和 Console URL。
4. 本次 Session 结束时使用 `Waiting external` Handoff。

这样可以关闭 Codex 窗口。远端任务仍会出现在 Today 的 Waiting external 中。

结果返回后：

1. 打开对应 Branch 的 Jobs。
2. 将已返回的 Job 标记为 Complete；失败原因记录在 Handoff 或 Activity 中。
3. 点击 `Update`，把 WorkItem 从 Waiting external 改回 Actionable/Active。
4. 点击 `Continue locally` 继续分析结果。

## Memory：什么内容值得让以后所有 Session 知道

原始聊天不会自动变成长期记忆。

适合记录为 Memory 的内容：

- 已验证的事实。
- 明确做出的技术决策。
- 可复用操作流程。
- 已确认的失败模式。
- 有 evidence 的实验结论。

在 Branch 顶部点击 Memory 图标，创建 proposed Memory。然后到左侧 `Memory` 页面进行 Accept 或 Reject。

只有 accepted Memory 会进入后续 Session Brief。接受 Memory 时必须具有来源 Branch 或 evidence reference。

不要记录：

- 临时聊天内容。
- 尚未验证的猜测，除非明确标记为 Hypothesis。
- Token、密码、API key 或其他凭据。
- 可以直接从代码或日志重新得到的大段原始内容。

### 每日总结与反思

左侧 `Memory` 默认打开 `Daily reflections`。系统在每天上海时间 00:00 启动一个独立、临时、
只读的 Codex Agent，复盘刚结束的上海自然日；它不会新建 WorkItem、Branch 或可恢复 Session，
也不会占用业务 Branch 的对话锁。

日报会综合系统当天能追踪到的 Web/本地 Codex 完成轮次、失败或中断的请求、Branch Result、
Jobs、Activity 和已记录 Memory，但按 `Work ID + 任务标题` 组织结果，而不是按对话轮次罗列。
它输出任务总览、今日完成、经验候选、不足、优化动作和明日焦点，每个重要判断都带 evidence ID。
turn/job 数只在报告底部作为覆盖诊断。未注册到 Research Ops 的终端工作无法读取全文，页面会明确显示
`unassigned windows` 和覆盖限制，不能把“未看到”理解成“没有发生”。

服务午夜不可用时，恢复后会自动补跑最近一个完整日期；也可以点击 `Run previous day` 手工
补跑，失败记录可在对应日期下点击 `Retry`。完成后会使用已有如流通知配置发送链接。

日报中的经验只是候选，不会自动进入后续 Session。需要长期生效的事实或决策，仍应切换到
`Memory ledger`，经过明确 Accept 后成为正式 Memory；可执行流程则在 `Skills` 中审核。

## Skills：查看已有能力并审核新提炼

Skill 和 Memory 的用途不同：Memory 保存事实、决策和实验结论；Skill 保存 Codex 下次可以照着
执行的操作流程。只有满足以下条件时才建议在 Handoff 勾选提炼：

- 这轮已经实际执行并验证了一个非显然流程。
- 流程以后会重复使用，且仅靠通用知识不容易稳定复现。
- 关键步骤能追溯到本轮 Handoff 或已同步的 final answer。

进入左侧 `Skills` 后默认显示 `Installed`：它会实时扫描 Codex 安装目录，列出 Personal 和
System Skill、frontmatter 中的触发描述、支持文件数量和安装路径。能在 Skill 源码目录找到
同名 Skill 时会标记 `Source linked`。这个页面只读；搜索、筛选、复制路径和刷新都不会修改文件。

切换到 `Review inbox` 后，会看到从完成任务中提炼出来的候选及三种判断：

| Action | 含义 |
| --- | --- |
| New | 建议新建一个独立 skill |
| Merge | 建议把新流程合并到明确归属的已有 skill |
| No reusable skill | 这轮没有值得沉淀的稳定流程 |

审核时先展开 `Review proposed diff`，检查证据、步骤和 Risks。点击 `Reject` 不会写文件；点击
`Accept and install` 后，系统才会校验 `SKILL.md`、更新技能源码目录并安装到 Codex 技能目录。
若校验或安装失败，Proposal 会显示为 Failed；已有 `SKILL.md` 会恢复到修改前内容。

默认部署使用：

```text
Skill source: /srv/research-ops/workspaces/alice/skills
Codex install: /root/.codex/skills
```

提炼过程使用临时的只读 Codex 执行，不创建用户 Session，也不会把临时任务 ID、Token、密码或
API key 写进 skill。接受前仍需人工检查 diff，因为 AI 负责提出候选，不负责最终发布决策。

## Result 和 Merge Queue

Branch 得到明确结果后，点击顶部的 Result 图标。

| Result | 含义 |
| --- | --- |
| Request merge to parent | 子 Branch 的结果值得回流到父 Branch |
| Park | 暂时保留，不继续，也不否定 |
| Abandon | 方案无效，不再推进 |
| Expire | 方案已经失去时效 |

选择 Request merge 后，它会进入左侧 `Merge Queue`。在 Merge Queue 中由人工决定 Merge、Park 或 Abandon。

注意：Merge Queue 首先合并的是 Research Ops 中的结果和分支状态。代码是否需要 Git merge，仍应结合 Branch 的 worktree 和实际提交进行检查。

## Repositories 和 worktree

有真实代码仓库时，先到左侧 `Repositories` 注册：

- Name：仓库名称。
- Canonical path：本机已有 Git 仓库的绝对路径。
- Default ref：通常填 HEAD 或主分支名。
- Programs：允许使用该仓库的 Program，例如 HARN,COMMON。

之后创建 WorkItem 时选择这个 Repository，系统会创建独立 worktree：

```text
Q3/PROGRAM/tasks/WORK-ID/primary/repository-name
Q3/PROGRAM/tasks/WORK-ID/forks/BRANCH/repository-name
```

如果不选择 Repository，系统会创建独立 Scratch Git 仓库，适合文档、实验脚本和临时研究。

## 本地终端是什么角色

本地终端是功能完整、优先级最高的 Codex 对话和执行入口。Web 是任务控制面和结果看板，也提供一次只执行一轮的轻量输入；需要审批、复杂交互或连续工作时仍应使用本地终端。

## 可选的 Pi 运行时

现在可以把 Pi 作为可选的 Agent Runtime。默认仍然是 Codex；创建单个 WorkItem 时，可在
**New work → Agent runtime** 选择 **Pi Agent**，其 Main Branch 会直接使用 Pi。未选择的 WorkItem
仍使用 Codex，不需要为了单个 Pi WorkItem 修改全局默认。

如果希望把部署的全局默认切换为 Pi，再明确配置：

```bash
export ROPS_CODEX_BACKEND=pi
export ROPS_PI_ENABLED=true
export ROPS_PI_PROVIDER=openai-codex
export ROPS_PI_MODEL=gpt-6-astra
export ROPS_PI_THINKING=max
export ROPS_PI_HTTP_PROXY=http://proxy.example.com:8891
export ROPS_PI_HTTPS_PROXY=http://proxy.example.com:8891
uv run rops server restart-when-idle
```

Pi 通过 JSONL RPC 运行，Web 可以使用 `steer`、`follow-up` 和 `abort`。只有收到完整的
`agent_settled` 后，这一轮才会被标记为完成。通过 New work 选择 Pi 时，创建的是原生 Pi
Main Branch，第一条 Web 消息会建立 Pi session，不包含 Codex 历史 handoff。Pi 的会话树不会
自动变成 Research Ops Branch，避免模型自行 fork 造成任务树漂移。

同一时间一个 Pi 会话只能由一个进程写入。Web 管理的 Pi RPC 会话不会同时交给本地 Pi TUI；
Pi Branch 页面会隐藏 Start/Continue locally，`rops open`
也会明确拒绝，避免拿 Pi session ID 误启动 Codex 或产生双写。当前请通过 Web 发起普通轮次，
并在运行中使用 `steer`、`follow-up` 和 `abort`。页面显示的 Pi session ID 只用于身份确认和
排障；后续加入显式 Release/Attach 后再支持本地接管。完整配置和回滚步骤见
[PI_RUNTIME.md](PI_RUNTIME.md)。

### 从 Codex 续接到 Pi

当一个 Codex Branch 已经有完成结果、当前没有活跃对话时，右上角会出现
**Continue with Pi**。点击后系统会：

1. 检查 Pi 是否可用。
2. 创建独立的 Pi 子 Branch 和独立工作目录。
3. 冻结来自父 Codex Branch 的 handoff：来源 thread/session、最近最多 6 轮已完成的
   问题和最终回答、以及最近的 Handoff。
4. 用新的 Pi session 打开子 Branch；父 Branch 在子 session 成功准备好后标记为 Continued。

Scratch work 不要求预先填写 Objective。若父 WorkItem 仍为空，系统会优先使用最近一次
Handoff 或已完成的用户请求生成一个限长的临时目标，因此不会因为“缺少任务目标”阻塞续接；这只是
Pi 本轮的工作上下文，不会冒充用户已经正式定义了 WorkItem。

不会把 Codex JSONL 直接当作 Pi JSONL 导入。两个协议格式不同，而且原始文件包含不应直接
进入新模型上下文的工具事件、审批事件和隐藏内容。Pi 收到的是经过脱敏和限长的历史参考，必须
先检查新工作目录并验证事实。父 Codex 对话仍可查看，新的 Pi Branch 在关系图中显示为子节点。

Codex 模式下，Branch 顶部的主按钮会复制类似命令：

```bash
'/path/to/research-ops/.venv/bin/rops' open 'BRANCH_ID'
```

这个包装命令会获取 Branch lease、启动或恢复 Codex、绑定真实 Session ID、每 15 秒 heartbeat、同步每轮最终回答，并在终端退出时结束 Session。`Raw resume` 只用于高级恢复；直接使用它不会获得这些管理能力。

也可以使用 Research Ops CLI：

```bash
cd /srv/research-ops/app
.venv/bin/rops status
.venv/bin/rops doctor
.venv/bin/rops open BRANCH_ID
```

终端和 Web 操作的是同一个 Branch 与 Codex thread，不是另一套系统。

## 三个方向的推荐组织示例

### HARN / PPT

```text
WorkItem: PresentBench PPT trajectory verifier
  ├── Main: deterministic verifier
  ├── Fork: vision-based verifier
  └── Fork: failed-trajectory repair
```

Memory 可以记录：可靠的轨迹格式、评测约束、已验证的失败模式。

### COST / 低精度与专家裁剪

```text
WorkItem: GLM FP8 paired experiment
  ├── Main: frozen BF16 baseline
  ├── Fork: FP8 candidate
  └── Fork: FP8 + optimizer quantization
```

将 LTP job 登记到 Jobs。不要为每个训练 job 单独创建 WorkItem，除非目标或验收条件已经不同。

### ADAPT / 新结构适配

```text
WorkItem: Qwen new architecture one-week adaptation
  ├── Child WorkItem: forward parity
  ├── Child WorkItem: checkpoint conversion
  ├── Child WorkItem: training smoke test
  └── Child WorkItem: evaluation and deployment
```

这里更适合使用 Parent work 拆分阶段任务，因为每个阶段都有独立交付物和验收条件。

## 可分享链接

页面 URL 会保留当前位置，可以直接收藏或发给团队成员：

```text
/today
/sessions
/programs/HARN
/programs/HARN/work/HARN-0001/brief
/programs/HARN/branches/BRANCH_ID/results
/merge
/memory
/repositories
```

刷新页面或使用浏览器前进、后退，不会丢失当前 Program、Branch 和标签页。

Results 中的 HTTP(S) 报告、论文和已部署页面会在新标签页打开，避免离开工作台。本地路径
（例如 `/mnt/.../index.html`）不是网页地址，因此显示为可复制的代码引用；点击旁边的复制按钮后，
可在同一服务器终端中直接使用该路径。

## 常见问题

### 显示 Expired，是不是工作丢了？

没有。Expired 只表示上一次 Session 租约结束。点击 Continue locally 会在同一 Branch 上继续，并保留 thread、文件、Results 和 Handoff。

### 我想到一个新点子，该 New work 还是 Fork？

- 最终仍回答同一个问题：Fork。
- 需要独立目标和验收：New work。
- 暂时不知道值不值得做：FREE + Capture only。

### 为什么不能直接把聊天都记住？

聊天里有大量临时判断、错误尝试和过时信息。只有经过审核的 Memory 才进入后续 Session，可以避免错误信息长期污染模型上下文。

### Start locally 复制的命令运行失败怎么办？

先运行：

```bash
cd /srv/research-ops/app
.venv/bin/rops doctor
```

确认 Codex available、workspace writable、frontend built 都正常。然后运行 `.venv/bin/rops open BRANCH_ID`，并查看终端错误与 Branch 的 Activity。

### Web 服务不在了怎么办？

Web 只是控制面，不是 Session 的唯一入口。先在服务器终端运行：

```bash
cd /srv/research-ops/app
.venv/bin/rops server status
.venv/bin/rops server start --host 0.0.0.0 --port 8020
```

`server start` 会启动脱离当前终端的 supervisor；Web 子进程异常退出后会在 3 秒后自动拉起。
常用维护命令：

```bash
.venv/bin/rops server restart
.venv/bin/rops server logs --lines 100
```

即使 Web 仍未恢复，也可以完全离线找到和恢复 Codex Session：

```bash
.venv/bin/rops sessions
.venv/bin/rops sessions --all
```

输出包含完整的 `Session ID`、`Branch ID`、目录和 Resume 命令。已纳管任务使用
`.venv/bin/rops open BRANCH_ID`，未分配窗口使用 `codex resume SESSION_ID`。这些命令直接读取
本地数据库和 `/root/.codex/sessions`，不依赖 8020。这套启动与恢复方式不使用 Docker 或镜像。

当前部署实例为了访问 Branch 外的 CFS 数据、集群工具和 LTP，使用
`ROPS_CODEX_SANDBOX=danger-full-access`。这意味着 Codex 以 Research Ops 服务用户的权限执行
命令；只应在受控的内部宿主机上使用，不要在公网暴露该实例，也不要在对话中提供凭据。

## 最简单的日常习惯

只记住下面七步就够了：

1. 早上打开 Today。
2. 点击一个 Ready/Expired 工作。
3. 点击 Start locally，在终端运行复制的命令并自然对话。
4. 有并行想法就 Fork。
5. 有远端任务就登记 Job。
6. 退出终端后 End this round，有稳定结论就提 Memory 或 Result。
7. 有可重复流程时勾选 skill 提炼，到 `Skills > Review inbox` 审核后再安装。
