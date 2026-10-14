---
name: octo-workflow
description: Visualize a requested task plan or local DAG in Octo and return a workflow link for Orbitree. Use when the user asks to draw, publish, or update a workflow visualization; not for routine conversation analysis, job execution, or goal supervision.
---

# Octo Workflow

Use the current conversation and the specific artifacts the user identifies to
build a useful workflow snapshot. Do not start a second model or analyze other
sessions. A visualization request authorizes publishing the diagram, not executing
its nodes, submitting jobs, changing Goals, or scheduling automatic updates.

## Create or Reuse

- If an appropriate Octo workflow already exists, read that exact ID and reuse its
  link. Do not duplicate it just to make it appear in Orbitree.
- Otherwise, identify meaningful stages and their real dependencies. Local DAG
  definitions can be translated into nodes and edges; unknown/unstarted stages
  stay `pending`. Only mark `succeeded` when the available evidence supports it.
- Write a small JSON artifact with a stable `job_id`, `nodes`, explicit `edges`,
  and optional `meta.title` / `meta.description`. Record uncertainty in node
  metadata, rather than inventing completion. Keep credentials, signed URLs,
  private reasoning, and full conversation dumps out of the artifact.

```json
{
  "job_id": "qwen-context-check-r1",
  "meta": {"title": "Qwen context check", "description": "Snapshot, not a job runner"},
  "nodes": [
    {"name": "prepare", "status": "succeeded", "meta": {"evidence": "config-validation.json"}},
    {"name": "evaluate", "status": "pending"}
  ],
  "edges": [{"source": "prepare", "target": "evaluate"}]
}
```

Run the bundled helper with the Orbitree Python environment, which already
contains `python-dotenv`. Resolve the helper relative to this skill directory:

```sh
/srv/research-ops/app/.venv/bin/python \
  scripts/octo_workflow.py validate --file /absolute/path/workflow.json

/srv/research-ops/app/.venv/bin/python \
  scripts/octo_workflow.py create --file /absolute/path/workflow.json
```

The helper reads `~/.config/research-ops/octo.env` (under `XDG_CONFIG_HOME` when set)
when present, otherwise the existing Orbitree `.env`. It accepts
`OCTO_BASE_URL` / `OCTO_API_KEY` or their `ROPS_` equivalents. `--env-file` selects
a different file; environment values take precedence. For an isolated Linux
user, provision an owner-readable Octo-only file rather than granting access to
the platform's entire `.env`. Never pass credentials in command arguments or put
them in the returned link. `CODEX_THREAD_ID`, when
present, is recorded as provenance for newly created workflows; it is not used to
change or attach a Orbitree session.

`create` reads the exact ID before writing. An identical existing definition is a
no-op; a different one is a conflict. It never deletes or overwrites an existing
workflow. A network error is not permission to create another ID: inspect the same
ID with `show --job-id ID` first.

## Update a Requested Snapshot

Read the exact workflow with `show --job-id ID`. For a requested status update,
write an update file and run `update --job-id ID --file /absolute/path/update.json`:

```json
{
  "current_stage": "evaluate",
  "node_updates": [{"name": "evaluate", "status": "running"}],
  "message": "Evaluation submitted; completion not yet verified."
}
```

Allowed statuses are `pending`, `running`, `succeeded`, `failed`. The helper uses
the server's `updated_at` for optimistic concurrency. A conflict requires a fresh
read and reconciliation, not a blind retry. It only updates Octo's visualization
records, not the underlying jobs. Topology changes can be made in Octo's UI; do
not recreate the workflow or reset statuses silently.

## Add Experiments to an Existing Workflow

Use `extend`, not `create` or a new workflow ID, when a requested visualization
needs more experiment nodes or dependencies. Put only the additions in a JSON
file; `prepare` below must already exist or also be included in `nodes`:

```json
{
  "nodes": [
    {"name": "baseline-a", "meta": {"description": "First formal baseline"}},
    {"name": "memory-095", "meta": {"description": "Memory utilization 0.95"}}
  ],
  "edges": [
    {"source": "prepare", "target": "baseline-a"},
    {"source": "prepare", "target": "memory-095"}
  ],
  "message": "Add independent experiment nodes"
}
```

```sh
/srv/research-ops/app/.venv/bin/python \
  scripts/octo_workflow.py extend --job-id WORKFLOW_ID --file /absolute/path/additions.json --dry-run

/srv/research-ops/app/.venv/bin/python \
  scripts/octo_workflow.py extend --job-id WORKFLOW_ID --file /absolute/path/additions.json
```

`extend` reads the current graph, merges additions, checks the full DAG, and calls
`/definition` with `expected_updated_at`. It keeps existing nodes, their metadata,
edges, and current stage, and does not submit status or timestamp resets. New nodes
start `pending`; use `update` separately when evidence establishes another status.
Node inputs accept only `name` and `meta`. An existing name with no metadata or
identical metadata is reused; different metadata is a conflict, not an overwrite.
Repeated nodes/edges already present cause no write. `--dry-run` only reads and
prints the planned nodes and edges. Edge-only additions are supported.

An HTTP conflict requires a fresh read and reconciliation. After a timeout, inspect
the same workflow before retrying; an already-applied identical extension is a
no-op. This command never removes or renames nodes, removes edges, or runs jobs.
Use explicit UI edits for those structural changes rather than recreating the graph.

## Return the Link

Include the helper's Markdown link in the assistant reply, outside code fences:

`[Workflow: descriptive title](http://OCTO_HOST/#workflow/WORKFLOW_ID)`

Orbitree extracts this link from recorded conversation text and displays it
in the Results workflow strip. No extra handoff, memory write, session registration,
or platform prompt is needed. The browser may require a separate Octo sign-in;
never embed the API key to bypass it. For a non-Octo visualization, use an HTTP(S)
link labelled `Workflow`, `DAG`, or `工作流` so the same strip can display it.

Do not imply live monitoring: the diagram changes only when explicitly updated by
this skill, Octo's UI, or an independently configured producer.
