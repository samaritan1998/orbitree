# Orbitree

Orbitree 是本地优先的研究执行工作台。它把长期方向、任务、分支、AI 会话、工作目录、外部任务和结果关联起来，让一项工作可以继续、分叉和回看。它不负责技术情报采集，也不要求从情报系统创建任务。

Web 用于查看工作树、可见对话进度和最终结果，也能提交一轮对话。本地 Codex、Claude Code 或 Pi CLI 保留各自的原生交互能力。普通消息原样交给所选运行时；Orbitree 不把 Brief、Memory 或平台规则自动注入提示词。跨运行时交接只有在用户明确选择后才附带上下文。

## 从哪里开始

1. 打开 `/today`。想法还不明确时选择 `Create scratch work`；目标明确时选择 `New work` 和工作方向。
2. 在任务的 `Results` 中直接发送一轮 Web 消息，或点击 `Start locally`，到同一服务器的终端运行复制的 `rops open` 命令。
3. 同一问题有独立方案时手动 `Fork`。如果只是继续当前工作，留在原 Branch；新问题则创建新的 Work。
4. 在 `Results` 回看每轮问题、可见活动和最终回答。需要长期保留结论时，使用 Handoff、Memory 或待审核的 Skill 提案。

完整操作见[中文使用手册](docs/USER_GUIDE.zh-CN.md)；部署、恢复和命令见下文。

## 功能导航

| 入口 | 适合做什么 | 边界 |
| --- | --- | --- |
| `/today`、`/programs/CODE` | 查看当前工作、方向树及 Graph/List 视图；创建、继续、隐藏或显式分叉任务 | Program 可在「设置 → 工作方向」增删和归档；任务目录不会因隐藏而删除 |
| `/sessions` | 发现本地 Codex 窗口，挂到任务并查看原生父子会话关系 | 只管理可识别的会话；Codex 内部 subagent 不作为独立窗口展示 |
| Branch 的 `Results / Brief / Goals / Jobs / Memory / Activity` | 对话、任务上下文、目标、外部运行、已审核记忆和状态记录 | Work 状态与 Session 状态分别显示；外部 Job 不等于 AI 轮次仍在运行 |
| 会话旁的时钟、`Goals` | 按间隔检查外部任务，或让 Codex 持续执行一个原生 Goal | 定时检查等待时不占模型；Native Goal 仅支持 Codex Branch，且 Active 不等于正在采样 |
| `/ltp-monitor`、`/clusters` | 查看个人 LTP 状态变化和全局资源视图 | LTP 监控须启用；群通知另需配置。Cluster Control 是只读模块，需要 `ltp-control` |
| `/evals` | 管理评测单元、Worker、重试和进度 | 需要配置可访问的评测 Worker 与 evaluator；不是通用聊天页面 |
| `/memory`、`/skills` | 查看每日复盘、审核记忆和可复用 Skill 提案 | 每日复盘和 Skill 提取可选启用；提案不会自动安装 |
| `/terminal` | 在浏览器使用一个独立的服务器终端 | 单人部署须显式启用并解锁；它不是 AI 会话，也不写入 Results |
| `/repositories`、`/merge`、`/settings` | 管理代码仓库、分支结果、方向和运行设置 | 多人账号入口需要单独部署，现有单人实例不会自动变为多人系统 |

窄屏下部分入口收在「更多」菜单。页面使用可分享的 URL；右侧结果区与工作树可以分别查看。

## 对话与自动化怎么选

| 需要 | 选择 |
| --- | --- |
| 问一个问题或执行一轮任务 | 在当前 Branch 用 Web 发送，或用原生 CLI 继续；同一 Branch 保持单写者 |
| 训练、部署等外部任务需要隔一段时间检查 | 设置 Scheduled check；自行填写检查提示词，等待阶段不启动模型 |
| 希望 Codex 持续推进一个明确目标 | 设置 Native Goal，并在 `Goals` 查看执行状态、恢复和历史 |
| 要并行试另一种方案或切换运行时 | 手动 Fork；可建立 Codex、Pi 或 Claude Branch，父分支保留 |
| 想每天回顾已登记的工作 | 启用 Daily reflection；报告显示在 `/memory`，不自动写入审核后的 Memory |

身份、模型和推理强度可按 Branch 选择；Codex 支持公司 API、个人账号和 OneAPI。Claude Code 需要所选服务器账号先在 CLI 登录。Pi 是可选运行时。登录凭据由相应 CLI 或环境变量管理，不进入任务描述或 Git 仓库。

## 文档索引

- 日常使用：[使用手册](docs/USER_GUIDE.zh-CN.md)、[服务恢复](RECOVERY.md)。
- 对话运行时：[身份切换](docs/identity-switching.md)、[Claude Code](docs/CLAUDE_RUNTIME.md)、[Pi](docs/PI_RUNTIME.md)、[Web 终端](docs/web-terminal.md)。
- 自动化与评测：[Native Goal](docs/NATIVE_GOAL.zh-CN.md)、[定时检查](docs/scheduled-checks.md)、[Agent Eval Harness](docs/AGENT_EVAL_HARNESS.md)。
- 团队与外部系统：[多人部署](docs/team-mode.md)、[统一任务提交](docs/MANAGED_JOB_CONTROL_PLANE.zh-CN.md)、[按需工作流可视化](docs/session-workflows.md)。

以下是安装、命令和运行边界的详细说明。

## Core records

- `Program`: personal directions managed in Settings; `config/programs.yaml` seeds new databases.
- `WorkItem`: durable work identity, emerging or defined objective, lifecycle, dependencies,
  acceptance criteria, and priority.
- `ExecutionBranch`: one selected agent runtime and an isolated workspace or real Git worktree.
- `SessionAttempt`: one leased execution window with a versioned brief and explicit handoff.
- `CodexWindow`: one locally discovered Codex TUI or resumable thread, optionally attached to work.
- `CodexTurn`: one recorded prompt, its final answer, timing, and owning Session; the model name of this record is historical.
- `MemoryEntry`: proposed or reviewed facts, decisions, procedures, failures, and hypotheses.
- `SkillProposal`: AI-extracted, evidence-linked reusable procedures awaiting explicit review.
- `InstalledSkill`: read-only metadata discovered from the configured local Codex skill directory.
- `ExternalJob`: LTP, Datawheel, evaluation, deployment, or other detached remote runs.
- `BranchResult` and `MergeReview`: explicit result return from a child branch to its parent.

## Local setup

```bash
scripts/install-host.sh --no-service
.venv/bin/rops server start
```

The default URL is `http://127.0.0.1:8020`.

For a hosted workspace, configure your own domain and authentication gateway.
Set `ROPS_INFOFLOW_PUBLIC_BASE_URL=https://research.example.com` to your actual
deployment URL for notification links; `research.example.com` is only a placeholder.
API requests and WebSockets follow the browser origin, including HTTPS/WSS. Server-local CLI
control and health checks should keep using `http://127.0.0.1:8020`, not the login gateway.

This installation uses no container and downloads no image. Remove `--no-service` to install and
start a user-level systemd service, or the detached auto-restarting supervisor when user systemd is
unavailable. Use `.venv/bin/rops server status|restart|logs` without opening the Web UI. The installer
never deletes project data or worktrees.
For a maintenance reinstall with an already valid `web/node_modules`, set
`ROPS_SKIP_WEB_INSTALL=1` to rebuild without re-extracting npm packages on CFS.

For development:

```bash
uv sync --extra dev
npm --prefix web install
uv run pytest
uv run ruff check backend tests
npm --prefix web run lint
npm --prefix web test
```

## Core commands

```bash
uv run rops programs
uv run rops desk HARN
uv run rops desk-context HARN
uv run rops new  # zero-input exploratory work in FREE
uv run rops new HARN --title "..." --objective "..."
uv run rops define FREE-0001 --title "..." --objective "..."
uv run rops status
uv run rops fork BRANCH_ID --name "..." --objective "..." --approved-by-user
uv run rops fork BRANCH_ID --name "..." --objective "..." --parallel --approved-by-user
uv run rops repair-fork BRANCH_ID --approved-by-user
uv run rops open BRANCH_ID
uv run rops attach BRANCH_ID  # print the managed open command
uv run rops windows  # scan local Codex TUIs and resumable threads
uv run rops adopt THREAD_ID --program FREE
uv run rops adopt THREAD_ID --branch BRANCH_ID
uv run rops handoff BRANCH_ID --conclusion "..."
uv run rops job submit /absolute/bundle --work COMMON-0007 --hardware b --kind inference
uv run rops job status LOGICAL_JOB_ID
uv run rops job list
uv run rops eval worker RUN_ID --evaluator-url http://127.0.0.1:8090/evaluate --slots 8
uv run rops server restart-when-idle
uv run rops doctor
```

## Unified job submission

`rops job` is the user-facing compute entry point. A user supplies a server-visible LTP bundle,
Work ID, task kind, and hardware class. There is deliberately no cluster option: Orbitree
validates the exact bundle, reads fresh capacity, chooses a hardware-compatible pool, records the
actual LTP identity internally, and returns only a logical job ID and logical state.

```bash
export ROPS_CONTROL_URL=http://127.0.0.1:8020
uv run rops job submit /absolute/path/to/bundle \
  --work COMMON-0007 --hardware b --kind inference
uv run rops job status LOGICAL_JOB_ID
uv run rops job list
```

The bundle directory must contain `config.yaml` and `run.sh`. Formal training also requires a
validated receipt through `--receipt`; `--train` may name a separate `train.py`. Repeating the same
command is idempotent because the CLI derives a stable key from the user, Work ID, task class, and
bundle bytes.

Logical intake is enabled by default, while real LTP mutation is disabled by default. Enable it only
on the control-plane host after validating its LTP identity and shared paths:

```bash
export ROPS_MANAGED_JOB_SUBMIT_ENABLED=true
export ROPS_MANAGED_JOB_ALLOWED_ROOTS='["/srv/research-ops/workspaces"]'
export ROPS_MANAGED_JOB_LTP_CLI=/absolute/path/to/ltp-cli
```

The trial CLI derives `submitter` from the local operating-system user. The API currently protects
requests with the instance access token but does not cryptographically bind that token to the
claimed user. A production multi-user rollout therefore needs trusted per-user authentication at
the API boundary before enabling unrestricted submission. The reviewed control-plane contract and
remaining rollout gates are recorded in
[`docs/MANAGED_JOB_CONTROL_PLANE.zh-CN.md`](docs/MANAGED_JOB_CONTROL_PLANE.zh-CN.md).

## Read-only cluster control

The `/clusters` workspace reads the all-user LTP inventory and current Tidal capacity through
`ltp-control live`. It does not submit, resize, stop, or delete jobs. Run `ltp-control` in an
environment that also contains the approved `ltp-cli`, or point Orbitree at a source checkout
and a Python interpreter where both dependencies are available:

```bash
export ROPS_LTP_CONTROL_SOURCE_ROOT=/absolute/path/to/ltp-control
export ROPS_LTP_CONTROL_PYTHON_COMMAND=/absolute/path/to/python
export ROPS_LTP_CONTROL_DIRECTORY_USER=bob
uv run rops serve --reload
```

`ROPS_LTP_CONTROL_ALLOW_PARTIAL_USERS` defaults to `true` for observability. A failed user query is
shown as `Partial`, with the missing user names and a coverage count; it is never presented as a
complete global scheduling snapshot. Set it to `false` when the caller should reject any incomplete
response. `ROPS_LTP_CONTROL_SNAPSHOT_PATH` can reference the last successful, sanitized
`ltp-control` snapshot as a read-only outage fallback. Optional runtime profiles are supplied with
`ROPS_LTP_CONTROL_PROFILES_FILE`; without a matching profile or framework receipt, the dashboard
shows ETA and Checkpoint fields as unavailable instead of deriving them from job age.

## LTP task monitor

`/ltp-monitor` is a separate per-user task monitor. It does not use the Cluster Control aggregate
as its source of truth and has no dependency on Work Items, Sessions, Goals, or Codex. It performs
only bounded read-only LTP CLI calls:

```bash
ltp-cli list --user alice --refresh
ltp-cli show <actual-ltp-job-name> --user alice
```

The list command discovers current task identities. The monitor then uses `show` for every newly
discovered and non-terminal task, because `show` is the lifecycle source of truth. If an active task
disappears from `list`, it is still confirmed with `show`; a missing row is never assumed to be
completed or failed. Every observed transition is stored in its own ledger. The default notification
states are `Created`, `Pending`, `Running`, and `Failed`; a state seen repeatedly produces one
notification, not one message per poll. The first successful scan establishes a baseline without
sending historic task alerts.

Enable it on the Orbitree host after confirming that the same service environment can run the
two commands above successfully:

```bash
export ROPS_LTP_MONITOR_ENABLED=true
export ROPS_LTP_MONITOR_USERS='["alice"]'
export ROPS_LTP_MONITOR_NOTIFICATIONS_ENABLED=true
export ROPS_LTP_MONITOR_GROUP_ID=12345678
export ROPS_LTP_MONITOR_POLL_INTERVAL_SECONDS=30
.venv/bin/rops server restart-when-idle
```

Group delivery uses the configured Infoflow/Mavin robot and shares only its credentials resolved
from `INFOFLOW_APP_KEY` and `INFOFLOW_APP_SECRET`; neither value is stored in the database, event
ledger, or notification body. The robot must have permission to send group messages and must be a
member of the target group. If `ltp-cli` fails, the monitor reports the source error in the page and
retains the last known task state rather than silently falling back to `ltp-control`. When a broken
or older LTP CLI cannot decode the current list response, point `ROPS_LTP_MONITOR_CLI` at
`scripts/ltp-cli-monitor`; this keeps `ltp-cli show` as status truth and only repairs list discovery.
You can also use `Track task` in the monitor page to register one exact LTP job name. An existing
`infoflow-message-group` credential file can be selected with
`ROPS_INFOFLOW_CREDENTIALS_FILE=/root/.infoflow_config`; Orbitree rejects group/world-readable
credential files and never writes their values to its database or logs.

## Scheduled checks

The clock beside the conversation composer opens a per-Branch schedule: choose an interval,
edit the exact check prompt, select manual/deadline/linked-job stopping, and set notification
preferences. Waiting uses no model process. Busy conversations defer checks; separate Branches
can check in parallel. Pause/Stop affect future checks, not the current turn or external jobs.
The dialog shows scheduler health, last/next checks, recent results and notification errors.
See [Scheduled checks](docs/scheduled-checks.md) for notification filtering, recovery and APIs.

## Workspace rules

Register an existing Git repository in the Web `Repositories` page, then select it when creating
work. New Program Desks and task workspaces use the human-readable Q3 layout:

```text
Q3/
  HARN/
    _desk/
    tasks/
      HARN-0002/
        primary/
        forks/data-diversity/
```

If no repository is selected, the branch receives an independent scratch Git repository. Program
Desk directories contain the direction charter and reviewed notes. Every writable task path remains
an isolated scratch repository or registered-repository worktree. Existing Branch rows retain their
stored `cwd`, but startup materializes a non-destructive Q3 symlink at the same managed location used
by new work. `Branch.workspace_path` exposes this human-readable entry while `Branch.cwd` remains the
original execution and evidence location. Occupied paths are never overwritten.

`Fork` snapshots the parent branch and prepares an independent Codex thread only after explicit
human approval. The default is **Fork and continue**: the parent path becomes `Continued`, and the
Web workspace moves to the child after the current parent turn finishes. Creating a fork never
automatically starts a child turn; use the child Branch's explicit continuation action when you
want it to execute. Use `--parallel` (or `Keep this branch active` in Web) when
the parent should remain available as the foreground conversation. If a persisted thread
cannot be forked, provisioning remains `Blocked` and can be retried with
`rops repair-fork BRANCH_ID --approved-by-user`. Orbitree never substitutes an empty
conversation for a failed native fork. `rops open` owns the Session lease, launches or resumes the native TUI,
binds a newly created Codex thread, synchronizes completed turns, and closes the Session when the TUI
exits. Raw `codex resume` is retained only as an advanced recovery command.

Child Branches can be soft-hidden from their inspector. Hiding never deletes the workspace, Git
branch, Codex thread, transcript, or lineage. Hidden intermediary Branches are collapsed in the
visible graph so their descendants remain connected to the nearest visible ancestor. Use the
Program header's `Hidden N` control to inspect and restore them.

The Web `Sessions` page also discovers Codex TUIs that were started outside Orbitree. It
correlates rollout metadata with local PID, TTY, and cwd without reading process environments or
Codex credentials. A discovered thread can attach to an existing Branch only when both use the same
directory, or create a WorkItem that references the existing directory without moving or editing
its files. The default Lineage view reconstructs native Codex parent/child relationships from the
persisted `forked_from_id`; this relationship is independent from Program ownership. When the parent
thread is already managed, `Attach under WORK-ID` imports the child as a Branch beneath the parent's
Branch while retaining the child's existing directory. Ignoring a window changes only dashboard
visibility and never stops the process.

Codex subagents are identified from the structured `source.subagent.thread_spawn` metadata. They
remain in the internal registry for traceability but are excluded from the Sessions API, counters,
and Lineage view. User windows receive durable display names: managed windows use their WorkItem and
Branch names, while unassigned windows can be titled from recent user requests in the background by
an ephemeral, read-only Codex CLI run. Manual names take precedence and are never overwritten. Attaching an unassigned
window uses this display name instead of creating a generic `Codex session · directory` title.

Work can begin before its title or objective is known. Web `Create scratch work` and zero-argument
`rops new` create an exploratory FREE WorkItem marked `needs_definition`. The managed Codex brief
allows natural exploration and asks the active Codex turn to call `rops define` only after intent,
boundary, and expected outcome are clear. The command is one-shot, so it cannot overwrite a task
that is already defined. Web creation is immediate and exposes an Undo action that only soft-hides
the WorkItem; its workspace and files are retained. Today and Program views poll the resulting durable state.

Each Program owns one hidden persistent Desk. It reuses the managed Branch runtime for leases,
Codex thread continuity, result synchronization, and Handoff, but is excluded from Today and the
task graph. `rops desk PROGRAM` opens it; `rops desk-context PROGRAM` returns a bounded snapshot of
visible WorkItems, Branch status, Handoffs, and latest final answers. Desk instructions forbid
editing child workspaces. Program-scoped Memory proposed from a Desk enters child Briefs only after
explicit review.

The managed terminal process renews the active Session lease every 15 seconds. A Web turn uses a
short-lived Session only when no terminal Session exists. Web input is serialized and remains locked
until that turn finishes. A local CLI turn always has priority: opening or using the managed CLI can
interrupt an in-flight Web turn, while Web waits for the synchronized local turn to complete.

Fork is optional and user-controlled. The current Branch remains the default place to continue
work; a broader or newly discovered idea does not require a fork. The agent may explain the
isolation tradeoff when it is relevant, but it must wait for the user's choice and must never
create or invoke a fork on the user's behalf.

Completed Web and local CLI turns can send an idempotent Infoflow single-chat reminder. Failed Web
queries can also send a separate alert with the Work, Branch, sanitized failure reason, and Results
link; this is enabled by default when notifications are enabled and can be switched off with
`ROPS_INFOFLOW_ERROR_NOTIFICATIONS_ENABLED=false`. Enable
`ROPS_INFOFLOW_NOTIFICATIONS_ENABLED`, set the recipient email prefix and public Orbitree URL,
and inject the AppKey/AppSecret through the environment-variable names configured by
`ROPS_INFOFLOW_APP_KEY_ENV` and `ROPS_INFOFLOW_APP_SECRET_ENV`. Notification delivery uses a durable
outbox: a provider outage never changes the Codex turn result, and successful sends are not repeated.
Configure the local ignored environment file without putting credentials in shell history:

```bash
scripts/configure-infoflow.sh alice https://research.example.com
.venv/bin/rops server restart-when-idle
```

Operational status is reconciled before Today, Program Tree, and Branch Detail responses. A WorkItem
cannot remain `running` without a live leased Session. Branch Detail presents durable Work status
separately from the latest Session status, so an executable `Work Ready` item can correctly show
`Session Expired` and offer `Continue locally`.

Web navigation uses stable History API routes, including `/programs/CODE`,
`/sessions`, `/evals`, `/skills`, `/programs/CODE/work/WORK-ID/TAB`, and
`/programs/CODE/branches/BRANCH-ID/TAB`. These routes can be
bookmarked, shared, refreshed, and restored with browser back/forward navigation. Program work can be
viewed as either an auto-fit graph or a compact list.

## Runtime boundary

The native Codex TUI executes unrestricted model turns inside branch-specific working directories.
The Results pane may submit one bounded Web turn against the same thread when the local CLI is idle.
A versioned
rollout reader under `backend/research_ops/codex` imports only completed turns into durable
`CodexTurn` records. File identity and byte cursors are committed with the imported turns, so service
restarts and transient database failures do not lose results. The local Codex App Server remains
limited to thread-lineage operations such as fork.

The system deliberately does not treat raw conversations as memory. Durable state changes happen
through WorkItem updates, Handoffs, External Jobs, proposed Memory, Branch Results, and Merge Review.
Only explicitly reviewed `accepted` Memory enters the display-only Session Brief. Main branches show current
accepted Program, WorkItem, and branch memory; forks freeze that inherited context and append only
newly accepted child-branch memory. Accepted entries require a source branch or evidence reference.
Persisted conversation text is passed through credential-pattern redaction.

### Conversation Transparency

Web queries and Goal objectives are sent verbatim. Orbitree does not prepend policies,
append developer/system instructions, inject Brief/Memory/Handoff context, or generate workspace
`AGENTS.md` files. Opening a local TUI does not send an initial task on the user's behalf.
Model, reasoning effort, and identity remain explicit user choices; native Codex configuration
owns skills, MCP servers, approval policy, and sandbox settings unless explicitly configured otherwise.
Briefs, recovery records, and cross-runtime references remain available in the UI, not in the prompt.

Existing conversation history is not rewritten. A missing transcript, compaction error, or large
JSONL file never triggers an automatic replacement thread. A new conversation requires an explicit
user action. The one-time `scripts/remove-generated-instructions.py` migration audits registered
workspaces and removes only unchanged platform templates when a backup directory is supplied.

### Agent Eval Harness

The `/evals` workspace manages durable rubric-level eval runs, atomic worker leases, exact retries,
cross-run cache reuse, quarantine, progress, throughput, and completion audits. PresentBench imports
expand both case and domain-common rubrics; generic agent suites can import their own stable units.
Workers can run as multiple replicas on cluster-d or another reachable cluster without manually
sharding the manifest. See [docs/AGENT_EVAL_HARNESS.md](docs/AGENT_EVAL_HARNESS.md) for the worker and
evaluator contracts.

### Optional Pi runtime

Pi can be selected as a global canary with `ROPS_CODEX_BACKEND=pi` and
`ROPS_PI_ENABLED=true`. Orbitree owns the Branch and Session state while Pi owns the model
loop. Independent Branches run concurrently by default; set `ROPS_CODEX_MAX_CONCURRENT_TURNS` to
a positive value only when an operator explicitly wants a deployment-level cap. Each individual
Branch remains single-writer. Pi's JSONL `steer`, `follow_up`, `abort`, and `agent_settled` events are normalized into the
existing activity and result records. The default remains the native Codex runtime; see
[PI_RUNTIME.md](docs/PI_RUNTIME.md) for proxy setup, controls, and the current one-writer session
boundary. An idle Codex Branch with completed results can also use **Continue with Pi** to create
an independent Pi child Branch. Orbitree freezes a bounded, redacted handoff from completed
Codex turns; it never attempts to load incompatible Codex JSONL as a Pi session.

### Claude Code runtime

Claude Code is available as a per-Branch runtime when the server's `claude` CLI is logged in.
Choose **Claude Code** in New Work, or use **Fork with Claude** on an idle Branch that already has
a completed turn. Web turns stream visible text and tool activity into the normal Results view;
**Start/Continue locally** opens the same Claude session through the native TUI. Orbitree stores
the session ID but leaves authentication under `~/.claude` entirely to the CLI.

Cross-runtime forks retain a bounded handoff as Branch metadata for auditability. The handoff is not
silently injected into the model. Ordinary messages are forwarded verbatim; an explicitly approved
Claude fork can attach its previewed conversation history to the first user message only. The source
Branch is unchanged. Claude and Pi Branches do not expose Codex Native Goal.

### Daily reflection

Set `ROPS_DAILY_REFLECTION_ENABLED=true` to create one evidence-backed reflection for each completed
calendar day. The default schedule is `00:00 Asia/Shanghai`; after downtime, the scheduler catches up
the most recent missing day. It uses a separate ephemeral, read-only Codex run, so it does not create
a Session, Branch, or workspace lock. Registered Web/CLI turns, jobs, results, activities, and memory
are included; unregistered or unattached local work is reported as a coverage limitation.

Reports appear under `/memory` in **Daily reflections**. Use **Run previous day** to backfill the last
completed day, or retry a failed date from its report. Reflections can suggest reusable lessons, but
they are not promoted into the reviewed Memory ledger automatically. When the existing Infoflow
notification integration is enabled, a completion message links back to `/memory`.

### AgentOS / Datawheel API

Orbitree integrates with AgentOS through HTTP, not through `miaoda-agent-sdk`. Enable the
adapter explicitly and provide the bearer token only through the referenced environment variable:

```bash
export ROPS_AGENTOS_ENABLED=true
export ROPS_AGENTOS_DATAWHEEL_BASE_URL='https://<agentos-host>/api/v1/datawheel'
export ROPS_AGENTOS_TRAJECTORY_BASE_URL='https://<trajectory-host>/api/v1/datawheel'
export ROPS_AGENTOS_TOKEN_ENV='DATAWHEEL_TOKEN'
export DATAWHEEL_TOKEN='<secret supplied by the runtime secret store>'
```

`POST /api/agentos/batches` submits an existing Datawheel payload with a required local
idempotency key. The remote `batch_task_id` is mirrored as an `ExternalJob`, while the local
iteration is stored as an `Execution`. `POST /api/agentos/sync` forces a status refresh and
`GET /api/agentos/trajectories/{app_id}` reads `coding_messages_all`. Background polling is enabled
by default when AgentOS is enabled. Request credentials, model API keys, records, and signed result
URLs are not written to durable Orbitree metadata; only a redacted routing snapshot, record IDs,
counts, and provider status are retained.

At Handoff, `Extract reusable skill for review` optionally starts an ephemeral, read-only Codex
analysis in the background. It compares the evidence with the configured skill catalog and proposes
either no skill, a new skill, or a merge into one existing skill. The proposal, source turn IDs,
risks, and full `SKILL.md` diff appear under `/skills`. Extraction never writes skill files. Only an
explicit `Accept and install` action validates the file, updates `ROPS_SKILL_SOURCE_ROOT`, and
atomically installs it under `ROPS_SKILL_INSTALL_ROOT`; rejection leaves both directories unchanged.

### OpenReward / ORS environments

The optional `ROPS_OPENREWARD_ENABLED=true` flag mounts an ORS-compatible
environment server at `/openreward`. The repository includes common `miaoda`,
`swe`, `ppt`, and `tooluse` adapters, a local mock server, and the backend
contract needed to plug in AgentOS, a SWE sandbox, SlideForge, or a tool
registry. See [docs/OPENREWARD_INTEGRATION.md](docs/OPENREWARD_INTEGRATION.md)
for the lifecycle, real-backend requirements, and the token/logprob boundary
for online MOPD. The deployment-neutral bridge protocol is documented in
[docs/OPENREWARD_RUNTIME_PROTOCOL.md](docs/OPENREWARD_RUNTIME_PROTOCOL.md).
The SWE runtime providers, isolation model, and task catalogs are documented in
[docs/OPENREWARD_SWE_RUNTIME.md](docs/OPENREWARD_SWE_RUNTIME.md).
Run `uv run python scripts/run_openreward_mock_e2e.py` for the four-domain
HTTP/SSE mock loop, and
`uv run python scripts/check_openreward_backend_contract.py` to inspect the
non-secret runtime contract. Miaoda's real reward-backed episode path is
Datawheel/AgentOS rollout followed by asynchronous Goblin Reward; evaluator
failures are kept distinct from a legitimate zero score. Pure MOPD stops after
the AgentOS episode, uses teacher token logprobs as the training signal, and
keeps the Slime schema reward at neutral `0.0` without invoking Goblin.
