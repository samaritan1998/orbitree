# Claude Code runtime

Research Ops can run a Branch with the locally installed Claude Code-compatible CLI. Authentication
remains owned by that CLI; Research Ops does not read, copy, or persist account credentials.

## Setup

Confirm the server login before enabling the runtime:

```bash
claude --version
claude
```

The default deployment settings are:

```dotenv
ROPS_CLAUDE_ENABLED=true
ROPS_CLAUDE_COMMAND=claude
ROPS_CLAUDE_MODEL=auto
ROPS_CLAUDE_EFFORT=high
ROPS_CLAUDE_PERMISSION_MODE=bypassPermissions
```

Use a different permission mode if the Web-managed process must not edit files or execute commands.

For this server's personal account, use the existing isolated launcher:

```dotenv
ROPS_CLAUDE_COMMAND=/root/.local/bin/claude-dmit
ROPS_CLAUDE_ACCOUNT_LABEL=Personal Claude
ROPS_CLAUDE_HOME=/root/.claude
ROPS_CLAUDE_MODEL=sonnet
ROPS_CLAUDE_EFFORT=high
ROPS_CLAUDE_PERMISSION_MODE=acceptEdits
```

The launcher selects the official Claude CLI, removes inherited API credentials/routing, and applies
the local DMIT HTTP proxy only to Claude and its children. Login remains in the CLI's own storage.

The root-owned `/etc/research-ops/claude-users.json` can additionally define an
`interactive_default` profile with `user`, `uid`, `gid`, `home`, `config_dir`,
`command`, and optional `workspace_umask`. With this explicit administrator
setting, bare `claude-dmit`, interactive `--resume`/`--continue`, and `auth status`
use the dedicated personal account and retain the caller's current directory.
Credentials stay in that user's private home; they are not copied back to root.
The caller's interactive permission flags and normal Claude settings are retained.
Changing the launch directory does not grant filesystem write access.
After dropping privileges, the launcher also creates a missing
`~/.local/bin/claude` link to the registered binary and includes that directory in
the child's `PATH`. No executable or credential is copied. Existing user
installations are left untouched; the managed launcher still validates and runs
the administrator-registered executable.

An exact entry in `sessions` still takes precedence and retains its registered
directory and permission mode. Unregistered `--print` or stream-mode invocations
keep the original fallback identity, so adding the TUI default does not silently
move existing Research Ops Web conversations to a different account. Deploy the
launcher to `/usr/local/libexec/research-ops-claude-user.py`; this change only
affects subsequent launches and does not require a Web service restart.

The official CLI rejects `bypassPermissions` under root. `acceptEdits` retains command permission
checks. Web tool requests use a bidirectional permission channel: the Branch displays the requested
operation and waits for **Allow once** or **Deny**. No global proxy is required.

## Use

1. In **New work**, select **Claude Code** as the Agent runtime.
2. Send a Web message normally. Results and visible tool activity stream into the Branch page.
3. Use **Start locally** or **Continue locally** to copy the managed `rops open` command and open the
   same session in the native Claude TUI.
4. From a completed Codex or Pi Branch, choose **Fork with Claude** to create an independent parallel
   Branch. Review the account/model and the optional conversation handoff before confirming.
   The source Branch stays usable. Creating a fork does not start a model turn.
5. From a completed Claude Branch, choose **Continue with Codex** to create a parallel Codex Branch.

The Web composer is single-writer per Branch. A local TUI takes priority over a pending Web turn, and
the two surfaces should not write to the same Claude session simultaneously.

### Branch model and effort

Choose the settings button beside **Next turn** in a Claude Branch. **Model** offers the stable
Sonnet, Opus and Haiku CLI aliases, plus a custom model ID. **Reasoning effort** offers `low`,
`medium`, `high`, `xhigh` and `max`. These are CLI presets, not account entitlement discovery;
the installed Claude version and selected model determine availability and effort support.
See the [official model configuration reference](https://code.claude.com/docs/en/model-config).

**Apply** saves only this Branch's next-turn settings. It does not change login, proxy routing,
global defaults, an in-flight Web request or an already open TUI. Web requests freeze the selection
when accepted; a new `rops open` launch passes it as native `--model` and `--effort` flags. The
selection is never appended to the user prompt. Scheduled checks retain the settings recorded when
the check was saved; save the check again to change its model.

`rops open BRANCH_ID --model opus --reasoning-effort high` can also set these Branch preferences.
Existing Branches without overrides continue to use the deployment defaults.

While a native TUI turn is running, its full interactive output remains in the terminal. When the TUI
exits, the managed wrapper imports completed user/assistant turns from that one Claude session JSONL
into Results. It does not scan or register unrelated Claude sessions.

## Prompt boundary

Ordinary user messages are passed to Claude verbatim. Branch briefs, Goals and platform rules are
never injected into the system prompt. An explicit cross-runtime fork is the exception: when
**Include conversation handoff** is confirmed, up to six completed source turns (a bounded excerpt,
not the full transcript or hidden reasoning) and their provenance are frozen in a private file.
The preview is checked again before creating the fork; a changed source requires another review.

That approved attachment is delivered as reference history in the first user message, followed by
the user's exact Web query. Results retain the original query; the attachment is separately visible
on the Claude Branch. Later turns do not reattach it. Starting locally sends the same attachment as
the initial TUI prompt; a resumed conversation uses Claude's saved history. No synthetic goal,
platform policy, or new execution objective is added. Unchecking the option creates a fresh Claude
conversation without importing source context.

Claude sessions are identified by UUID and persisted by the CLI under its normal `~/.claude` storage.
Research Ops keeps only the session identifier and normalized Results/Live activity records.

## Workspace handoff and approvals

For a scratch-workspace Claude fork, Research Ops snapshots small source files and documentation
into the independent child directory, including uncommitted files. The private
`.research-ops-workspace-snapshot.json` records source paths, hashes and exclusions. Existing child
files are never overwritten. Git metadata, credentials, dependencies, model weights, caches and
raw experiment outputs are excluded. Profile directories contribute only shallow source/config
files, not nested payloads. Files are limited to 2 MiB each and 128 MiB in total.

An existing Claude fork can be repaired via `POST /api/branches/{id}/claude-workspace-handoff`
with `{"manual_approval": true, "take_over": true}`. Both Branches must be idle. The existing Claude
thread and its conversation remain unchanged; the parent becomes `continued`, its future scheduled
checks are paused, and its files and external jobs are not modified or stopped. The operation does
not send a prompt or start an experiment.

Only the child directory is a default writable workspace. References to parent files or external
model paths do not grant access automatically. Claude permission requests are shown with the tool,
arguments and blocked path. One approval authorizes one requested operation with its original
arguments; it does not persist broad permissions or enable bypass mode. After interruption or a
server restart, pending requests expire and cannot be replayed against a new process. Native TUI
approvals remain in the terminal. The transport follows Claude's
[official control protocol](https://github.com/anthropics/claude-agent-sdk-python/blob/main/src/claude_agent_sdk/_internal/query.py).
