# Claude Web Wakeups and Workspace Access

Research Ops persists successful `ScheduleWakeup` calls observed in a live
Claude Web turn. The one-shot wakeup appears under the existing clock control,
separately from user-configured periodic checks. No historical JSONL is imported
into the scheduler.

The source turn must finish successfully. The worker then sends the tool's exact
prompt to the same Branch and Claude session, retaining the original execution
settings. A busy session is deferred without interrupting it. An accepted message
is linked atomically and is never resubmitted after a crash. An unaccepted claim
can be recovered after five minutes.

A new manual Web message, an interrupted source turn, a replacement wakeup, or
the clock's cancel action cancels pending work. Hidden/inactive Branches and
replaced sessions cannot be woken. Wakeups more than 24 hours overdue are marked
missed instead of unexpectedly restarting old work. Read the status and reason
under the clock. Stopping a wakeup does not stop external jobs.

The existing `ROPS_SCHEDULED_CHECKS_ENABLED` switch and scheduler heartbeat apply
to wakeups. This bridge applies to Web turns, not independent Claude TUI
processes. Merely saying "I will check later" does not create a wakeup.

## Dedicated Linux Users

The root-owned `/etc/research-ops/claude-users.json` may opt an explicitly
registered session into these fields:

```json
{
  "workspace_umask": "0007",
  "artifact_read_roots": ["/approved/workspace/artifacts/specific-run"]
}
```

The default umask remains `0077`. The optional `0007` permits sharing within the
dedicated private group, not with all local users. The account home remains
`0700`; credentials are not copied, modified, or exposed by this feature.

Before dropping privileges, the launcher reconciles group read/search access
inside the administrator's exact artifact allowlist. This handles root-owned
`0600` receipts written by remote jobs even when the shared filesystem does not
inherit a directory's group. It changes ownership group and permission metadata
only, preserves the owner, and grants no new group write permissions. It does not
change remote job scripts or their explicit umask.

The scan is bounded to five seconds, 5,000 entries, and eight levels. It skips
symlinks, hardlinks, unrelated users/groups, hidden files, model weights, caches,
environments, and non-report file types. It does not copy artifacts or scan Q3.
Files created while a turn is running are reconciled at the next launch.

Before each change, original metadata is appended to a private root-owned audit
file under `/var/lib/research-ops/claude-permissions/<uid>.jsonl`. Review this audit
to restore individual permission grants if required. A scan-limit warning means
the allowlist should be narrowed or explicitly reviewed, not broadened to a
workspace root.

After changing launcher source, install it as the root-owned file used by the
`claude-dmit` wrapper. This is independent of a Research Ops server restart and
only affects subsequent process launches. Server/UI wakeup changes require the
usual tested release and an idle restart.
