# Manual Codex identity switching

Research Ops has three explicit Codex identities: **Company API**, **Personal Codex**, and
**Company OneAPI**.
Existing branches default to Company API. Pi authentication is independent and is not changed.

## First-time personal login

Run this on the Research Ops server, using the installed `rops` command:

```sh
/srv/research-ops/app/.venv/bin/rops auth login
```

Complete the device-code flow in your own browser. Never send API keys, login codes, or
`auth.json` contents through a Research Ops conversation.

The command creates an isolated personal Codex home at
`$ROPS_CODEX_HOME/research-ops/personal` (normally `~/.codex/research-ops/personal`).
It does not replace the existing `~/.codex/auth.json` or `config.toml`, and it does not
change any branch selection. Your existing plain `codex` windows retain their configuration.

The personal home shares session transcripts, skills, and writer locks with the primary
home. SQLite state uses the same explicit `sqlite_home`. This preserves thread IDs and
the single-writer rule while keeping personal credentials, model caches, and processes separate.
No task directory or saved transcript is moved, deleted, or automatically forked.

To check setup without exposing credentials:

```sh
rops auth status
```

"Credentials present" is a local check, not a guarantee of current quota, model access,
or OAuth validity. A provider rejection is surfaced as an error; it never triggers an identity change.

### Device-code login returns 403

A 403 before a device code appears is a failure to obtain a code, not proof that your
account credentials are invalid. Check the server's egress before repeating login.
Personal Codex can use a separate proxy for both login and subsequent execution:

```dotenv
ROPS_CODEX_PERSONAL_HTTP_PROXY=http://proxy.example:8891
ROPS_CODEX_PERSONAL_HTTPS_PROXY=http://proxy.example:8891
ROPS_CODEX_PERSONAL_NO_PROXY=localhost,127.0.0.1,::1
```

Use an approved proxy for your environment. These settings override the corresponding
lowercase and uppercase proxy variables in Personal Codex child processes only. They do
not change Company API, Pi, or the parent Web service environment. Include internal
hosts in the bypass list as needed by your tools. When unset, shell proxy settings remain
unchanged. A fresh `rops auth login` reads the new configuration immediately; restart an
idle Research Ops service before using the changed proxy for Web turns.

On this server, the initial device-code request returned 403 with direct egress and
offered a device code through the configured deployment proxy. That confirms the login
request path, not successful account authorization or model access. The deployment uses
that proxy only for Personal Codex.

Device-code login must also be enabled in your ChatGPT security settings or workspace
permissions. If the flow remains unavailable, the official alternatives are browser
login with SSH forwarding or securely transferring a locally created login cache. For
Research Ops, any personal cache belongs under the isolated personal home described
above, never over the primary API `auth.json`. Do not send login codes or credentials in chat.

## Change a branch

1. Open the branch's Results tab.
2. Select the identity/model control above the message box, next to **Next turn**.
3. Choose Company API, Personal Codex, or Company OneAPI, then select a model and reasoning effort.
4. For a switch to Personal Codex, confirm that this branch's conversation and work context may
   be processed by your personal account. Ensure this is permitted for your work data.
5. Select **Apply**, then send your next message as usual.

The selection survives page refreshes and server restarts. Cancel does not save or send anything.
If Personal Codex is not logged in, Apply remains disabled and the login command is available.
Refresh login status after completing login. The model list loads only when the settings dialog is
open, uses the selected identity's catalog, and is cached for five minutes. Use the refresh
icon beside Model to reload it. Listing models does not create or resume a conversation.

For models with capability metadata, Reasoning effort contains the advertised values,
including `ultra` where supported. When you choose a different model, an incompatible draft effort
is replaced by that model's advertised default. Nothing is persisted until you select Apply.
Personal Codex always needs an explicit model choice. OneAPI pre-fills its configured default,
`DeepSeek-V4.1-Flash`; this remains a draft until Apply. No pending message is rerouted.

OneAPI reads its own `/models` endpoint instead of the OpenAI catalog. This endpoint does not
advertise reasoning capabilities, so its effort control offers manual protocol values:
`none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`, and `ultra`. The initial default
is `high`; a saved or manually chosen value is preserved across model changes unless explicit
capability metadata excludes it. These values are not a claim that every model supports each
effort; the provider may reject or ignore an unsupported setting. OneAPI also permits an
explicit custom model ID.

Company API also offers an explicit **Custom model...** entry for gateway models not listed in the
Codex catalog. Its reasoning options are manual protocol settings, not verified model capabilities.
Personal Codex uses the discovered list only. A catalog entry is not a completed inference test or
a guarantee of remaining quota. Loading failures stay visible and can be retried without changing
the branch selection or blocking the existing conversation.

## Local terminal

**Start locally** and **Copy terminal command** use `rops open`, so they carry the same branch
identity and inject credentials only into the child process environment. Raw `codex resume`
continues to use your shell's own Codex configuration, not the platform selection.

You can also explicitly change the branch preference while opening it:

```sh
rops open BRANCH_ID --identity chatgpt --model ACCOUNT_MODEL --reasoning-effort high
rops open BRANCH_ID --identity api --model API_MODEL --reasoning-effort max
rops open BRANCH_ID --identity oneapi
```

A TUI already running keeps its launch identity. Exit and reopen it to use a newly selected
identity. Changing the preference never terminates a running Web turn or local TUI.

## Execution and audit rules

- Each accepted Web query stores a non-secret identity, endpoint, model, and effort snapshot.
- Model-capacity and transport retries reuse that snapshot; they never select another identity.
- Different branches may execute concurrently with different identities. One thread still has
  only one active writer. There is no new global concurrency limit.
- Native Goals retain the identity captured when they were started. Changing **Next turn** does
  not change an existing Goal. Clear the Goal and explicitly create a new one to change its identity.
- Persistent-worker Goals likewise retain their execution snapshot.
- Results and pending queries show the recorded execution identity. Local JSONL model/effort
  observations take precedence over the launch model when available. Historical records without
  identity evidence are not backfilled with the current selection.
- Daily reflection, skill extraction, and session-title generation remain on Company API.
  A branch identity selection does not reroute those background tasks.

Changing identity reuses the same thread. If the selected provider cannot resume or compact its
history, the error remains visible. Research Ops does not silently change identity, throw away the
history, or fork the task to work around an authentication error.

## API credentials and operations

### Company OneAPI

Configure these values in the server's private environment or mode-0600 `.env` file:

```dotenv
ROPS_ONEAPI_BASE_URL=https://models.example.com/v1
ROPS_ONEAPI_API_KEY=
ROPS_ONEAPI_MODEL=DeepSeek-V4.1-Flash
ROPS_ONEAPI_REASONING_EFFORT=high
```

Supply the key privately, not through a branch message or a committed configuration file.
This is an independent `rops-oneapi` provider using the Responses protocol. It does not
overwrite Company API credentials, Personal Codex login, or the global default model.
The OneAPI hostname bypasses the personal egress proxy. Other identities do not inherit its key.
An unavailable provider fails visibly instead of falling back to another account.

Codex may report missing built-in model metadata for a non-OpenAI model. This warning is not a
failed turn. Text generation and shell tool execution were verified for `DeepSeek-V4.1-Flash`;
image input and long-history compaction are not certified by that smoke test.

### Existing Company API

Company API runs use a custom provider with `env_key = "ROPS_CODEX_API_KEY"` and
`requires_openai_auth = false`. Personal runs use the built-in `openai` provider and enforce
ChatGPT login. Inherited API/access-token/workload-identity environment overrides are removed
from personal child processes. The parent server environment is not mutated.

The existing API login remains supported. To store or rotate a separate API credential, set
`ROPS_CODEX_API_KEY` securely in the command environment and run `rops auth import-api`.
It writes a mode-0600 credential file at `~/.codex/research-ops/api-auth.json`, outside the codebase.
The key never appears in a command argument, branch preference, event payload, or API response.
`auth login` also preserves the existing API credential when available.

Configuration using inline provider authentication commands or header credentials must first be
migrated to an environment-key provider. An unknown provider is rejected, not redirected to a
different endpoint. Deployments using keyring-only personal authentication need the explicit
file-backed personal login above; the Web service does not read arbitrary keyring entries.

Backend changes require the normal Research Ops service update. Once deployed, changing a branch
identity or refreshing personal login does not require a Research Ops restart.

Official references: [Codex authentication](https://learn.chatgpt.com/docs/auth),
[Codex environment and state locations](https://learn.chatgpt.com/docs/config-file/environment-variables).
