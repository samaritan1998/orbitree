# Pi Runtime

Research Ops can run Pi as an optional JSONL/RPC agent runtime. Codex remains the default
runtime and the existing Work/Branch/Session data model remains authoritative. Independent
Independent Branches run concurrently by default; set `ROPS_CODEX_MAX_CONCURRENT_TURNS` to a
positive value only when an operator explicitly wants a deployment-level cap. A single Branch
remains single-writer.

## Enable a canary

Copy the relevant values into the deployment environment (or the ignored local `.env` file):

```bash
export ROPS_CODEX_BACKEND=pi
export ROPS_PI_ENABLED=true
export ROPS_PI_COMMAND=pi
export ROPS_PI_PROVIDER=openai-codex
export ROPS_PI_MODEL=gpt-6-astra
export ROPS_PI_THINKING=max
export ROPS_PI_HTTP_PROXY=http://proxy.example.com:8891
export ROPS_PI_HTTPS_PROXY=http://proxy.example.com:8891
```

The proxy variables are copied only to Pi child processes. They are not written to Codex
configuration, Research Ops traces, or training jobs. `ROPS_PI_EXTENSIONS_ENABLED` is false by
default; enable extensions only for a trusted local canary because Pi extensions have full host
permissions.

Restart the service and verify the runtime without sending a model request:

```bash
uv run rops server restart-when-idle
curl -s http://127.0.0.1:8020/api/health | jq '.codex'
```

The health payload should report `protocol: "pi-rpc-jsonl"`, the configured model, and
`proxy_configured: true`.

## Runtime behavior

Each managed Pi session is a long-lived `pi --mode rpc` subprocess. Research Ops sends JSONL
commands and consumes only normalized events:

- `prompt`: a normal Web turn;
- `steer`: deliver a correction after the current tool batch;
- `follow_up`: queue a message after the agent settles;
- `abort`: stop the current operation;
- `agent_settled`: the authoritative end of the complete run, including retries and queued work.

The control endpoints are:

```text
POST /api/branches/{branch_id}/runtime/steer
POST /api/branches/{branch_id}/runtime/follow-up
POST /api/branches/{branch_id}/runtime/abort
```

An explicit control command is recorded as a `runtime_control` conversation item and an Activity
event. It does not become a pending ordinary Web turn.

## Select Pi for a new WorkItem

The **New work** dialog has an **Agent runtime** selector. It defaults to Codex; selecting
**Pi Agent** creates the WorkItem's Main Branch with `runtime=pi`. This is a Pi-native Branch, so
there is no Codex transcript handoff and its first Web message creates the managed Pi session.

A normal Codex deployment can leave `ROPS_CODEX_BACKEND=cli` and `ROPS_PI_ENABLED=false` while
creating selected Pi WorkItems. The Pi command, provider, model, authentication, and proxy still
need to be valid. Pi-native WorkItems use the Web composer and runtime controls; local
Start/Continue remains unavailable to prevent two writers from sharing one Pi session.

Omitting `runtime` from the API remains backwards compatible and uses the deployment default:

```json
{
  "program_code": "HARN",
  "title": "Pi-native experiment",
  "objective": "Run the experiment with Pi",
  "branch_mode": "write",
  "runtime": "pi"
}
```

## Continue a Codex Branch with Pi

`Continue with Pi` is an explicit Branch action, not a model-initiated fork. It first verifies that
Pi is available and that the parent Codex Branch has no active turn. It then creates an independent
child workspace and a new Pi session. The parent becomes `Continued` only after the child Pi session
is ready.

Pi cannot resume a Codex JSONL file directly: their session formats and event semantics differ.
Instead, Research Ops freezes a redacted, bounded handoff in the child Branch context. It contains
the source Branch/thread/session identity, up to six completed Codex prompt/final-answer pairs, and
the latest durable handoff. The snapshot is hashed for provenance and is supplied to Pi as untrusted
historical reference material. Pi must inspect the child workspace and verify facts before acting.

The transition is visible as a normal child Branch in the Program tree:

```text
Codex parent (read-only / Continued) -> Pi continuation (new workspace / new session)
```

The original Codex thread remains available for inspection. Its raw rollout is not copied into the
Pi session, and its hidden reasoning, approval events, and tool transcript are never promoted into
the Pi prompt.

## Current MVP boundary

The global `ROPS_CODEX_BACKEND=pi` option remains available for a full Pi canary. In the normal
Codex deployment, both a Pi selection in **New work** and **Continue with Pi** enable only their
explicit Pi Branches; sibling Codex Branches keep their runtime. Pi RPC and a local Pi TUI must
not write the same JSONL session concurrently. Research Ops keeps ownership of managed Pi sessions, so the local Start/Continue
action is hidden for Pi Branches and `rops open` fails explicitly instead of launching Codex with a
Pi session ID. Use the Web composer and the `steer`, `follow-up`, and `abort` controls for Pi.

The displayed Pi session ID is an identity and troubleshooting reference, not a concurrent-resume
invitation. Explicit Release/Attach support is intentionally deferred until Research Ops can hand
off ownership and import local Pi results without two writers touching the same session.

Pi's internal session tree is not automatically promoted to a Research Ops Branch. A future
session registry will attach a Pi session explicitly and will require an explicit user action to
promote a Pi conversation fork into a new Branch.

## Rollback

Unset `ROPS_PI_ENABLED`, restore `ROPS_CODEX_BACKEND=cli`, and restart the service. Pi sessions
remain in `~/.pi/agent/sessions`; no existing Codex session or Research Ops workspace is removed.
