# 多人使用 Orbitree

多人入口采用邀请制。每个账号对应一个 Linux 执行用户、一个 Orbitree 实例和一套私有目录。各实例共享代码，不共享会话、数据库或模型登录信息。无需 Docker。

## 当前部署边界

本次先交付独立入口和用户实例管理。原有 `8020` 单人实例没有迁移，原目录和会话保持不变。不能仅在前面加一个登录页，就把现有未鉴权的实例当作多人系统开放。

正式邀请同事前，管理员必须完成以下检查：

- 公开域名只转发到账号网关；用户实例只监听各自的 Unix socket。
- 若保留旧实例，旧入口必须限制为管理员可访问。不能让同事通过旧 IP、端口或旁路服务访问原账号。
- 将原账号接入统一入口需要单独的维护窗口：停止旧监听后，在原目录和原数据库上启动受鉴权保护的实例。不要同时启动两个会监督目标或发送通知的旧账号实例。
- 尚未做企业 SSO、跨用户共享项目、磁盘和 CPU 配额、自动安装模型 CLI 或线上旧账号切换。账号网关当前由服务器管理员运行，应放在可信 HTTPS 入口之后。

这些账号隔离的是同事之间的普通访问，不防服务器 root 管理员。Linux 用户不是虚拟机，网络仍可访问服务器允许访问的服务；共享主机上的其他内部服务仍须自行鉴权。

## 目录结构

```text
/opt/research-ops-team/releases/<版本>/   只读代码、前端、Python 环境和公共工具
/srv/research-ops-team/
  control/                              账号、会话、审计和启动策略，仅管理员可读
  users/<账号>/                         0700 私有目录
    config/settings.json                本用户实例配置
    config/user.env                     本用户自己的可选环境变量
    data/                               本用户数据库和运行记录
    work/<方向代码>/tasks/              本用户的任务与分支目录
    .codex/                             本用户的 Codex 登录和会话
    .claude/                            本用户的 Claude 登录和会话
    .pi/                                本用户的 Pi 数据
    run/api.sock                        私有服务入口
```

新用户的 Linux 名称为 `rops_<账号>`。不授予 sudo，系统登录默认禁用。Agent 及其子进程受 Landlock 文件访问规则约束：自己的目录可写，明确授权的共享目录只读，未授权目录不可读写。内核不支持所需规则时，启动失败，不降级成无隔离运行。

其他用户的 `/proc/<PID>/cmdline` 也不开放，避免泄露命令参数中的对话内容。依赖扫描全机进程的发现功能会受限，首次接入本地 TUI 或额外运行时应单独验收；不要为此开放整个 `/proc`。

## 准备公共代码

在源码目录执行。使用新的版本目录，不覆盖正在运行的版本。

```bash
cd /path/to/research-ops/web
npm ci
npm run build
cd ..
.venv/bin/python scripts/prepare-team-release.py /opt/research-ops-team/releases/v1 --frontend web/dist
uv venv /opt/research-ops-team/releases/v1/.venv --python python3.13
cd /opt/research-ops-team/releases/v1
uv pip install --python .venv/bin/python '.[team]'
```

发布脚本只复制后端代码、构建后的前端和包元数据，不复制 `.env`、数据库、用户目录或登录凭据。为各 Agent 安装的公共 CLI 放入版本目录的 `tools/bin`，不要链接到 `/root` 下的个人安装或配置。工具版本由管理员选择并验证。

## 创建账号

以下命令由服务器管理员执行。将 `alice`、显示名称、版本目录和域名替换成实际值。首次创建自己的管理账号时，在 `add-user` 后增加 `--admin`。

```bash
rops team add-user alice 'Alice'
rops team provision alice --release /opt/research-ops-team/releases/v1
rops team start-user alice
rops team invite alice https://research.example.com
```

最后一条命令返回一次性激活链接，24 小时内有效。只把链接发送给对应同事；链接等同临时登录凭据，不写入工单或公共日志。同事打开链接后自行设置至少 12 位密码。链接使用 URL fragment，页面接收后立即从地址栏移除，不进入 HTTP 请求日志。

首个管理员账号仍通过终端创建。账号网关配置 `--release` 后，管理员可在「设置 → 团队账号 → 新增用户」中创建同事账号：

1. 填写账号名和显示名称。账号名为 3–27 位小写字母、数字或下划线，以字母开头。
2. 点击「创建并生成邀请」。系统按管理员指定的版本创建普通 Linux 用户、私有目录和独立实例。
3. 等待实例通过健康检查，再复制邀请链接给对应同事。同事自行设置密码，模型登录信息也由本人配置。

网页不能指定执行命令、版本路径、共享目录或管理员角色。创建过程不会复制原账号的会话、数据库、API Key 或模型登录信息。普通成员不能新增用户或管理邀请。

若初始化失败，账号列表会显示失败状态和「重试初始化」。系统保留已创建的目录和初始化记录，重试不会覆盖已有文件；实例未就绪时不生成邀请。重复点击、请求断开或网关重启后，先刷新账号列表，再对原账号重试，不要另建同名账号。实例已启动但尚未通过健康检查时，重试会检查原实例，不重复启动。

未配置 `--release` 时，「新增用户」不可用并显示原因；已有的终端创建流程不变。网页仍可为已准备的账号生成邀请、停用或恢复访问。停用会撤销登录会话和未使用的邀请，但不删除文件，也不终止外部训练任务。

## 启动入口

```bash
rops team serve \
  --origin https://research.example.com \
  --frontend /opt/research-ops-team/releases/v1/web/dist \
  --release /opt/research-ops-team/releases/v1 \
  --host 127.0.0.1 --port 8030
```

将 HTTPS 反向代理的上游设为 `127.0.0.1:8030`，同时转发 WebSocket Upgrade。`--origin` 必须与浏览器地址完全一致。公网不允许明文 HTTP 登录；本机验收可使用 `http://127.0.0.1:8030`。

网关重启与用户实例分开管理。网关不自动重启全部用户实例；已有后台实例继续运行，浏览器实时连接会重连。可用 Supervisor 管理启动命令，示例见 [team-supervisor.conf](../deploy/team-supervisor.conf)。当前服务器没有可用 systemd，不依赖它。

## 配置模型身份

进入对应用户的受限环境，再执行用户自己的登录操作：

```bash
rops team shell alice
codex login
# 或按用户选择执行 Claude / Pi 的登录命令
```

公共 CLI 必须已安装。该 shell 的 `HOME`、`CODEX_HOME`、`CLAUDE_CONFIG_DIR` 和临时目录均指向 Alice 的私有目录；不继承管理员进程的 API Key、代理或其他环境变量。

需要 API Key 或代理时，放到本用户 `config/user.env`，权限保持 `0600`。不要复制管理员的整个环境文件。通知默认关闭；在本用户 `config/settings.json` 配置自己的接收人、凭据文件和通知开关后，重启该用户实例。修改这些运行配置不影响其他人。

## 配置工作方向

新账号默认只有 `Free Work`。在「设置 → 工作方向」中新增方向，设置代码、名称、颜色、目标和排序。方向代码为 2–16 位大写字母或数字，以字母开头。

代码用于目录、任务编号和链接，创建后不修改。名称、颜色、目标和排序可以调整。归档会从导航移除方向，不删除任务和文件；恢复后可继续使用。至少保留一个启用的方向。重启不会再用默认清单覆盖个人设置。

## 共享数据

只在创建用户时为明确的数据目录授权只读访问：

```bash
rops team provision alice --release /opt/research-ops-team/releases/v1 \
  --shared-read /mnt/shared/datasets/team-eval
```

不要授权整个 `/mnt`、`/workspace`、他人的主目录或凭据目录。共享数据通过路径授权使用，不复制大型模型、缓存或实验产物。已有用户的授权修改需管理员更新 `control/<账号>.json` 并重启该用户实例，不能由 Agent 放宽。

## 运维与排障

```bash
rops team users
rops team start-user alice
rops team stop-user alice
```

`stop-user` 可能中断该用户的 Web 对话，应先确认其空闲。它不删除文件。用户实例日志在 `control/<账号>.log`，权限为 `0600`。

- 无法登录：确认邀请未过期、账号未停用；连续失败会限流 5 分钟。
- 忘记密码：管理员执行 `rops team reset-login alice https://research.example.com`，撤销旧网页登录并生成新的激活链接。工作区和模型登录信息不变。
- 登录后工作区不可用：确认用户实例已启动，检查其私有日志和 Unix socket，不要临时开放 TCP 端口绕过鉴权。
- 文件权限失败：确认路径属于本用户目录或明确的共享授权。不要直接改成 root 执行或关闭访问限制。
- 模型不可用：确认已安装 CLI，并在对应用户环境中登录；账号网关不会代填模型凭据。
- 恢复部署：停止新网关和受影响的新用户实例，保留其目录；恢复原入口配置。不要删除旧工作目录或把多个实例同时指向同一数据库。

账号库和工作库应分别备份。运行中的 SQLite 使用数据库备份 API，不只复制单个 `.db` 文件。`control` 包含密码哈希和内部服务令牌，备份必须限制访问；用户目录还可能含模型凭据。

文件隔离基于 [Linux Landlock](https://docs.kernel.org/userspace-api/landlock.html)，Python 接口使用 [py-landlock](https://github.com/SebastienWae/py-landlock)。网络、资源配额与 root 管理员权限不属于本次文件隔离保证。
