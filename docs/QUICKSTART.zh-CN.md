# Orbitree 快速上手

Orbitree 组织 Codex、Claude Code 或 Pi 的实际工作，管理任务、目录、会话、实验和结果。它不是技术情报采集系统，也不要求先从日报创建任务。

## 先认识界面

### Program 工作树

![Program work tree](../research-ops/docs/images/program-tree.png)

- 左侧 Program 是长期方向，例如 HARN、COST、ADAPT。
- 中间节点是 WorkItem，一个可持续推进的实际问题。
- 子节点是显式 fork 的独立尝试；它们保留父子关系和各自的目录、结果与 Handoff。
- 右上角可在 `Graph` 与 `List` 间切换。任务较少时 List 更紧凑，分支较多时 Graph 更直观。

### 一条任务的执行视图

![Live activity](../research-ops/docs/images/live-activity.png)

- `Results`：每一轮最终回答与被中断的 query。
- `Live activity`：本地 CLI 或 Web turn 的可见更新和命令状态，不展示隐藏推理。
- `Jobs`：LTP、部署、Datawheel、评测等外部运行。
- `Memory`：经过审核后可进入后续 Brief 的事实、决定、失败经验和步骤。
- `Activity`：状态变更和排障线索。

## 一次完整工作流

### 1. 从想法到工作

有一个值得验证的问题时，进入 Orbitree：

- 已经知道方向和目标：点击 `New work`，选择 Program，填写一句 Title。
- 只有模糊想法：点击 `Create scratch work`。它会在 `FREE` 中创建临时工作，不要求先写目标。

不要为了“有想法”就先建 fork。Fork 的用途是隔离同一个 Work 内的另一种实验路径。

### 2. 用本地终端真正执行

进入 Work 的 Results 页：

1. 点击 `Start locally` 或 `Continue locally`。
2. 复制系统给出的 `rops open ...` 命令。
3. 在同一台服务器的终端运行该命令。
4. 在当前 Branch 选择的原生 Codex、Claude Code 或 Pi 界面中对话、执行命令并查看输出。

Web 不是本地终端的替代品。Web 可提交一轮受管对话，也会汇总本地执行的结果；不同运行时的本地结果同步时机并不完全相同。本地 CLI 保留完整交互能力。

### 3. 何时 fork

同一目标下，出现一个不希望干扰当前路径的假设时才 fork。例如：

```text
Main: 规则校验 PPT 编辑轨迹
Fork: 用视觉模型做布局重叠校验
```

- 希望父分支暂停并转到子分支：选择默认的 `Fork and continue`。
- 希望两条路径并行：选择 `Keep this branch active`。
- 新的目标完全不同：创建 `New work`，不要 fork。
- 只是下一轮继续现有工作：使用 `Continue locally`，不要 fork。

每一个 fork 都需要用户手动点击。系统不会让 Agent 自动把一个想法变成分支。

### 4. 训练和评测不等于会话仍在运行

提交 LTP、部署或评测后，把任务登记为 `Waiting external`，并在 `Jobs` 查看状态。此时可以结束本地 Codex Session，不需要让终端窗口一直开着。

当结果返回时，在同一 Work 或同一 Branch 继续即可。不要因为 Session 显示 `Expired` 就重新建一个 Work：它仅表示上一轮终端已结束，工作及其目录、结果、记忆和外部 Job 都还在。

### 5. 结束一轮，保留可继续上下文

退出本地 Codex 后，点击 `End this round`，至少记录：

- `Conclusion`：已验证或已完成的内容。
- `Next action`：下一次应从哪里继续。
- `Outcome`：继续、等待外部、阻塞或等待审查。

若这一轮形成了可复用流程，勾选 `Extract reusable skill for review`。它只会创建一个候选 Skill；只有你在 `Skills` 页面审核接受后，才会写入本地 Skill 目录。

## 目录约定

Orbitree 默认使用可读的 Q3 布局：

```text
Q3/
  HARN/
    _desk/
    tasks/
      HARN-0001/
        primary/
        forks/vision-verifier/
```

系统会为新任务创建目录。已有目录不会被移动、覆盖或删除；Attach 现有 Codex 会话时也只建立管理关联。

## 常见状态

| 页面状态 | 含义 | 下一步 |
| --- | --- | --- |
| Work `Ready` / Session `Expired` | 工作能继续，上一次终端已结束 | `Continue locally` |
| Work `Running` | 当前有受管运行或外部活动 | 看 Live activity / Jobs |
| `Waiting external` | 训练、部署或评测正在远端运行 | 看 Jobs，不必保持聊天窗口 |
| `Needs decision` | 需要人工决定方向或参数 | 更新状态后继续 |
| Web `Request blocked` | 模型网关的连接或并发问题，不等于任务内容被拒绝 | 稍后重试；检查同时运行的 Web/local Codex 数量 |

## 启动和恢复

```bash
cd research-ops
.venv/bin/rops server status
.venv/bin/rops server start
.venv/bin/rops server restart-when-idle
.venv/bin/rops doctor
```

Orbitree Web 暂时不可用时，Work 和会话仍保留在本机。Codex 会话可在原目录使用 `codex resume`；Claude Code 和 Pi 使用各自的原生恢复方式。服务恢复后，再从 Sessions/Results 继续管理。详细步骤见 [RECOVERY.md](../research-ops/RECOVERY.md)。

其他功能的入口、启用条件和边界见 [Orbitree 功能导航](../research-ops/README.md#功能导航)。Sessions 挂载、Handoff 与 Memory 的详细操作见 [使用手册](../research-ops/docs/USER_GUIDE.zh-CN.md)。
