# On-Demand Workflows

Workflow visualization is provided by the `octo-workflow` skill. Research Ops
does not automatically analyze conversations, invoke an analysis model, or
inject workflow instructions into a conversation or Goal.

Ask the active agent to visualize a task, for example:

> Use $octo-workflow to visualize this experiment's stages and dependencies.

The skill reuses an existing Octo workflow or publishes a snapshot from the
current conversation and explicitly identified local DAG files. Publishing a
diagram does not execute its nodes or supervise the underlying jobs. Updates
occur only on request, or through a separately configured producer.

## Results Links

The existing Results workflow location is now a compact link strip:

- Octo deep links: `http://OCTO_HOST/#workflow/WORKFLOW_ID`.
- Other HTTP(S) links labelled `Workflow`, `DAG`, `工作流`, or `流程图` in Markdown.
- Previously associated Octo workflow links remain available.
- No links means no empty strip. Links open in a separate tab. Octo may require
  a browser sign-in; API credentials are never embedded in a link.

The read-only endpoint `/api/branches/{id}/workflow-links` extracts links from
that Branch's recorded user/assistant messages and turn results. It never follows
URLs, calls a model, writes a conversation, or reads hidden reasoning. It ignores
code blocks, inline code, images, unrelated links, and credential-bearing URLs.
Duplicates collapse to one link. The bounded projection considers the latest 200
workflow-link candidate turns and 200 candidate messages, with a 512,000-character budget
and at most 64 distinct links. The UI reads this projection every 30 seconds while
visible; its refresh button only repeats the GET.

Include the workflow link in the assistant reply outside a code block. Tool
stdout alone is not a recorded user-facing workflow link.

## Skill Installation

The versioned source is `skills/octo-workflow`. Install it in the desired Codex
home's `skills` directory, or symlink that directory to this source. The current
server installation uses `/root/.codex/skills/octo-workflow`.

`scripts/octo_workflow.py` provides `validate`, `create`, `show`, and `update`.
Validation is offline. Create is idempotent for the same definition and refuses
to overwrite conflicting content. Update uses Octo's optimistic concurrency
field and does not blindly retry. The helper never executes jobs or deletes
workflows. See the skill's `SKILL.md` for examples.

The helper reads `ROPS_OCTO_BASE_URL` and `ROPS_OCTO_API_KEY` from the existing
Research Ops `.env`, overridden by environment variables. It also accepts
`OCTO_BASE_URL` / `OCTO_API_KEY`, `--env-file`, and `--base-url`. Do not pass keys
on the command line or commit the private `.env`.

## Migration

The automatic observer and analysis workers are no longer started, even with
old enable flags. `POST /api/branches/{id}/workflow/observe` returns HTTP 410 so
old browser bundles cannot reenroll a session. Existing analysis records remain
in the database for reference; they are not rerun or shown as current failures.
No native session, Goal, job, Handoff, or Memory record is rewritten.

Tests cover the passive link projection, disabled worker lifecycle, helper API
operations, idempotency, credential protection, and UI behavior. The fixture
browser check never contacts production or creates an Octo workflow:

```sh
cd web
npm run build -- --outDir dist-workflow-links
cd ..
node scripts/check-session-workflow-ui.mjs web/dist-workflow-links
node scripts/preview-session-workflow.mjs web/dist-workflow-links 5175
```
