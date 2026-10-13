# Research Ops Recovery

浏览器入口由部署者配置；`https://research.example.com/` 仅为示例域名。
服务器本地的健康检查及 CLI 控制地址为 `http://127.0.0.1:8020`。如果部署者配置了登录网关，未登录请求的跳转不代表后端服务停止。

Web 不可访问时，在服务器终端执行：

```bash
cd /srv/research-ops/app
.venv/bin/rops server status
.venv/bin/rops server start --host 0.0.0.0 --port 8020
```

重新启动已有服务：

```bash
.venv/bin/rops server restart
.venv/bin/rops server logs --lines 100
```

Web 无法恢复时，离线查找完整 Session ID、Branch ID 和恢复命令：

```bash
.venv/bin/rops sessions
.venv/bin/rops sessions --all
```

已纳管任务用输出中的 `rops open BRANCH_ID`；未分配的历史窗口用
`codex resume SESSION_ID`。这些命令不依赖 Web 服务，也不会修改或删除工作目录。
