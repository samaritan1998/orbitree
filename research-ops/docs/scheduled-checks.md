# Scheduled Checks

Open a Branch's Results page and select the clock next to the conversation composer.
Set an interval (5/10/30/60 minutes or 1-10080 custom minutes), replace the task placeholder
in the editable prompt, choose a stop condition, and select **Start schedule**.
The first check runs after the interval. **Check now** makes it due on the next scheduler tick.

Opening an editable schedule also drafts a context-specific prompt through the configured
Company OneAPI model (`ROPS_ONEAPI_MODEL`, default `DeepSeek-V4.1-Flash`). It reads only this
Branch's most recent eight recorded query/result pairs and up to sixteen newer user/assistant
messages, within a 22,000-character evidence budget. Tool logs, hidden reasoning, workspace
files and sibling Branches are excluded. Current prompt instructions are included separately,
so explicit permission to handle issues or a restriction against resubmission can be retained.
Credential patterns are redacted before sending. This is a model-generated suggestion, not an
independently verified task-state report; review the task identity and scope before saving.

Drafts use one or two conversational Chinese sentences, usually 30-80 characters, plus a
separate check-result format line. They refer to the current task naturally instead of repeating
paths, parameters, history or a checklist. Explicit user permissions and restrictions remain;
existing saved schedules are never rewritten automatically to adopt this style.

New schedules are filled automatically unless the user has already typed. Existing prompts and
manual edits remain unchanged: **Apply suggested prompt** explicitly adopts the new suggestion.
The sparkle icon drafts again from recent context. Identical requests are coalesced and cached
for five minutes; new conversation records or a changed prompt invalidate the cache. An active
check keeps the form read-only and does not request a draft. Closing the dialog ignores late
responses. OneAPI errors/timeouts leave the prompt editable and show an explicit retry option.

Drafting consumes OneAPI quota but does not start a Codex/Pi turn, take its writer lock, create a
schedule, change identity preferences, or insert a message into the original conversation. It
does not use the conversation's selected API/personal identity. Requests have a 45-second total
deadline and no automatic provider retries. The generated text remains an editable draft until
**Start schedule** or **Save schedule** is clicked.

DeepSeek drafting requests explicitly use `thinking: {"type":"disabled"}` so the 2,048-token
output budget is available for the prompt, not consumed by a reasoning-only response. This
request-local option does not change the original conversation's model or reasoning settings.
Truncated output, empty text, rejected credential-like text and malformed responses have
distinct errors. Logs contain only these fixed error descriptions, never response bodies or
reasoning. Failure never switches to Company API or Personal Codex.

The prompt is sent verbatim as an ordinary conversation turn. There are no hidden instructions,
Goal creation or automatic Handoffs. Only creating or updating a schedule captures the Branch's
current identity/model/effort; later changes to conversation preferences do not switch an existing
schedule's billing identity. Results retain the query and are labeled **Scheduled check**.
This is periodic agent execution, so each actual check consumes the selected model's quota;
waiting between checks does not. The default prompt asks for one read-only check without
resubmitting, deleting or modifying an external task. Editing that prompt changes what you ask
the agent to do; this feature does not add a separate tool-permission sandbox.

## Scheduling And Stops

- One schedule per Branch, one in-flight check per schedule. Separate Branches run independently.
- The next interval starts after the previous result is recorded, not after its start.
- An active Web turn, local CLI turn or active Goal defers checking. No interruption, backlog of
  missed intervals, or hidden global model-slot limit is added.
- Schedules persist in the database and run with the server even when the browser is closed.
- Pause/Stop prevent future checks. They do not stop a current agent turn or an external task.
  Use the existing conversation interrupt control to explicitly interrupt a running check.
- A deadline is entered in Shanghai time (UTC+8) and stored as UTC. It prevents subsequent checks;
  it does not cancel a check that started before the deadline.
- A linked job must belong to the same Work item. Terminal states stop checks only with an
  observation less than 15 minutes old. For LTP, a newer observation in the existing LTP monitor
  is used by exact job name and configured user. Other jobs need their registered status updated
  by the existing job integration/API. Stale or unknown states do not claim completion.
- A deleted linked job or hidden/inactive Branch pauses the schedule for review.

The dialog shows last/next check, waiting reason, scheduler heartbeat, recent check results and
notification delivery errors. A sleeping schedule is not a running conversation.

## Notifications

Notifications use the existing Infoflow/Ruliu credentials and `ROPS_INFOFLOW_RECIPIENT`.
Both `ROPS_INFOFLOW_NOTIFICATIONS_ENABLED=true` and working credentials are required.
The UI warns when notification delivery is not configured. No credentials belong in prompts.

The default prompt explicitly requests a final line in this form:

```text
检查结果：无变化
```

The other supported values are `有进展`, `失败`, `完成`, and `需要决定`.
Only this visible final-answer verdict controls filtering; tool logs do not. The default prompt
defines important changes as phase/state changes, errors, a significant stall or a decision,
not ordinary training-step increments. This classification is model-reported, not a guarantee.

- **Changes and action needed** suppresses no-change results and identical repeated notices.
  Important progress, task failure/completion, decisions, check errors and schedule ending notify.
- **Every check** includes no-change results.
- **Off** disables these notices, including check errors.

When an edited prompt omits the verdict, the result is marked **Unclassified result** and sent
for review rather than silently discarded. Keep the visible verdict request for quiet filtering.
The ordinary per-turn notification is suppressed for a scheduled check, so it is not sent twice.
Notifications have durable, retryable outbox records and include the task title, Branch, session
ID, Shanghai timestamp and Results link.

## Failure And Recovery

Check execution errors delay subsequent checks by 2x then 4x the configured interval. Three
consecutive errors pause the schedule. An interrupted check pauses future checks immediately.
No task submission is retried by the scheduler itself. Runtime transport handling remains the
same as an ordinary Web turn.

If a server restart leaves a claimed check without a confirmed query, the scheduler waits five
minutes and pauses for review instead of blindly replaying it. A completed request without a
recorded final answer also pauses after five minutes. Accepted queries/results retain their
existing durable conversation records. Resume explicitly after reviewing an ambiguous result.

## Operations

```dotenv
ROPS_SCHEDULED_CHECKS_ENABLED=true
ROPS_SCHEDULED_CHECK_TICK_SECONDS=15
```

Startup creates `scheduled_checks`, `scheduled_check_runs` and `scheduled_check_notifications`.
No existing conversations are scheduled automatically. There is no external scheduler process
or new dependency. The worker performs short database transactions and sleeps between ticks;
it never holds a transaction while waiting for a model or an external job.

Endpoints use the existing write authorization:

```text
GET  /api/branches/{branch_id}/scheduled-check
PUT  /api/branches/{branch_id}/scheduled-check
POST /api/branches/{branch_id}/scheduled-check/control
POST /api/branches/{branch_id}/scheduled-check/prompt
```

The control body is `{"action":"pause"}`, `resume`, `stop` or `run_now`.
The PUT body contains `prompt`, `interval_minutes`, `stop_mode`, `stop_at`, `job_id`, and
`notification_mode`. Stop modes are `manual`, `deadline`, `job`; notification modes are
`important`, `all`, `none`. Only deadline mode accepts `stop_at`; only job mode accepts `job_id`.
The prompt endpoint accepts `{"current_prompt":null}` or the current draft text and returns the
draft, provider/model, context counts and timestamps. It has no database write side effects.

Regression tests: `tests/test_scheduled_checks.py`, `tests/test_scheduled_check_prompts.py` and
`web/src/components/ScheduledCheckControls.test.tsx`.
The browser acceptance script `scripts/check-scheduled-check-ui.mjs` uses mocked APIs and never
sends a real prompt, submits a job or sends a Ruliu message.
