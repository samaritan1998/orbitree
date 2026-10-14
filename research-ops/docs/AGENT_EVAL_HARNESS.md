# Agent Eval Harness

The Eval Harness is a durable control plane for rubric-level agent evaluation. It keeps scheduling
state in Orbitree while evaluator processes remain stateless and replaceable.

## Execution model

```text
task manifest -> EvalRun + stable EvalUnits -> leased worker batch -> evaluator
                       ^                              |                |
                       +--- retry/cache/audit <-------+--- result -----+
```

Each unit key covers the candidate artifact digest, case, rubric, rubric/input digest, and semantic
configuration hash. A successful unit is reused across runs only when that complete key matches.
Worker count, endpoint placement, batch size, and retry count do not affect the semantic hash.

- `canonical`: one rubric per evaluator request. Scale with worker slots or worker replicas.
- `fast`: one claim batches rubrics sharing a material key. This lets an evaluator load or inspect a
  candidate once and answer several rubrics.
- `tool_parser` defaults to `true` and is part of the semantic configuration hash.
- Missing candidate artifacts and exhausted failures are quarantined, never converted into zeroes.
- Expired leases become retryable, or quarantined after `max_attempts`.
- Result and config payloads are redacted; credentials must be supplied to workers through
  environment variables.

## PresentBench run

Create the run in `/evals`, or use the API:

```bash
curl -X POST "$ROPS_URL/api/eval-runs" \
  -H "Authorization: Bearer $ROPS_ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "work_item_id": "HARN-0004",
    "name": "candidate-r1 PresentBench",
    "adapter": "presentbench",
    "mode": "fast",
    "candidate_id": "candidate-r1",
    "tool_parser": true,
    "batch_size": 16,
    "max_attempts": 3,
    "config_json": {"judge_protocol": "presentbench-rubric-v1"}
  }'
```

Then import the task manifest and candidate result root:

```bash
curl -X POST "$ROPS_URL/api/eval-runs/$RUN_ID/import-presentbench" \
  -H "Authorization: Bearer $ROPS_ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"task_file\":\"$TASK_JSONL\",\"result_root\":\"$RESULT_ROOT\",\"auto_start\":true}"
```

The adapter reads each case's `generation_task/judge_prompt.json`, expands the
`material_dependent_checklist_*` arrays, and locates the candidate at
`RESULT_ROOT/OUTPUT_DIR/slides.pptx`. It also fingerprints `slides.pdf`, the task query, reference
materials, case rubrics, and domain-common rubrics so cached judgments cannot cross artifact or
prompt changes. Legacy records without `presentbench_case_dir` derive it from the parent of the
`generation_task` directory in `query_path`.

The standard `check_slide_count` callable is evaluated deterministically during import. It remains
one of the strict answer units, but it never consumes a judge request; other callable rubrics are
forwarded to the evaluator with `rubric_kind=code`.

PresentBench summaries expose both the raw unit mean and the official aggregate. The official value
first averages each rubric class, gives the five classes equal weight within a case, and then averages
complete cases. Partial cases are excluded until every expected unit in each class succeeds.

## Worker deployment

Run one or more workers on any cluster that can reach Orbitree and the evaluator. One process
can expose several independent slots:

```bash
export ROPS_ACCESS_TOKEN="..."
export EVAL_SERVICE_TOKEN="..."

rops eval worker "$RUN_ID" \
  --control-url "$ROPS_URL" \
  --evaluator-url "http://127.0.0.1:8090/evaluate" \
  --evaluator-token-env EVAL_SERVICE_TOKEN \
  --worker-id "train-z-replica-01" \
  --slots 8 \
  --claim-size 16
```

Deploying the same command in several cluster-d instances increases concurrency without partitioning
the manifest manually. Unit claims are atomic, and a reclaimed instance's leases are recovered.

The evaluator receives:

```json
{
  "run": {
    "id": "...",
    "adapter": "presentbench",
    "mode": "fast",
    "candidate_id": "candidate-r1",
    "config_hash": "...",
    "config_json": {},
    "tool_parser": true
  },
  "units": [
    {
      "id": "...",
      "lease_token": "...",
      "case_key": "academia/...",
      "rubric_key": "material_dependent_checklist_1:0000",
      "material_key": "...",
      "input_json": {
        "candidate_uri": "/path/to/slides.pptx",
        "reference_paths": [],
        "rubric": "..."
      }
    }
  ]
}
```

It returns one result per unit:

```json
{
  "results": [
    {
      "unit_id": "...",
      "score": 1.0,
      "result_json": {"answer": "yes", "evidence": ["slide 4"]},
      "duration_ms": 820,
      "cache_hit": false
    },
    {
      "unit_id": "...",
      "error": {
        "category": "judge_timeout",
        "message": "request timed out",
        "retryable": true
      }
    }
  ]
}
```

The worker renews leases while the evaluator request is active and submits only the failed unit for
retry.

## Generic adapter

Non-PresentBench suites can import units through `POST /api/eval-runs/{run_id}/units/import`.
Every unit needs a `case_key`, `rubric_key`, `input_json`, and either a per-unit
`candidate_digest` or a run-level digest. Set `material_key` to group units that can share loaded
artifacts in `fast` mode.

## Audit gate

`GET /api/eval-runs/{run_id}/audit` is valid only when the expected unit count matches, successful
scores are in `[0, 1]`, every expected unit succeeded, no lease is expired, and no unit is
quarantined. A PresentBench run with
`tool_parser=false` also reports a configuration warning. Treat this audit as the completion gate
before publishing aggregate metrics.
