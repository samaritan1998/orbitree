from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from research_ops import models
from research_ops.codex.workflow_analysis import WorkflowAnalyst
from research_ops.db import run_db
from research_ops.octo import OctoClient
from research_ops.services.codex_events import redact
from research_ops.services.core import NotFoundError
from research_ops.settings import Settings
from research_ops.workflow_schema import WorkflowAnalysis, normalized_status


def _iso(value: datetime | None) -> str | None:
    return value.replace(tzinfo=UTC).isoformat() if value else None


def _text(value: Any, limit: int = 1200) -> str:
    return redact(str(value or ""))[:limit]


def _exact_reference(value: str, records: str) -> bool:
    return len(value) >= 8 and bool(
        re.search(r"(?<![\w-])" + re.escape(value) + r"(?![\w-])", records)
    )


def _matches(meta: Any, branch_id: str, thread_ids: set[str], job_ids: set[str]) -> bool:
    if not isinstance(meta, dict):
        return False
    entries = [meta] + [
        meta[key] for key in ("research_ops", "rops") if isinstance(meta.get(key), dict)
    ]
    for entry in entries:
        for key, allowed in (
            ("branch_id", {branch_id}),
            ("thread_id", thread_ids),
            ("codex_session_id", thread_ids),
            ("session_id", thread_ids),
            ("external_job_id", job_ids),
            ("remote_job_id", job_ids),
        ):
            values = entry.get(f"{key}s", [])
            if not isinstance(values, list):
                values = []
            if any(
                isinstance(value, str) and value in allowed for value in [entry.get(key), *values]
            ):
                return True
    return False


def _node(
    name: str, status: str, refs: list[str], *, associated: bool = True, detail: str = ""
) -> dict[str, Any]:
    return {
        "id": name,
        "title": _text(name, 120),
        "status": normalized_status(status),
        "raw_status": _text(status, 80),
        "status_basis": "observed",
        "evidence_refs": refs,
        "detail": _text(detail),
        "associated": associated,
    }


class SessionWorkflowService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        octo: OctoClient,
        analyst: WorkflowAnalyst,
    ):
        self.session_factory = session_factory
        self.settings = settings
        self.octo = octo
        self.analyst = analyst
        self._last_octo_poll = 0.0
        self._octo_synced_at: str | None = None
        self._octo_error: str | None = None

    def observe(self, branch_id: str, *, retry: bool = False) -> dict[str, Any]:
        with self.session_factory() as session:
            if session.get(models.ExecutionBranch, branch_id) is None:
                raise NotFoundError("Branch not found")
            row = session.get(models.SessionWorkflow, branch_id)
            if row is None:
                row = models.SessionWorkflow(branch_id=branch_id)
                session.add(row)
            elif retry and row.analysis_status == "error":
                row.analysis_status = "queued"
                row.analysis_error = None
                row.analysis_attempts = 0
                row.next_analysis_at = models.utc_now()
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                if session.get(models.SessionWorkflow, branch_id) is None:
                    raise
        return self.view(branch_id)

    def retry_legacy_http_failures(self) -> int:
        """Requeue failed HTTP-adapter projections once after moving to the CLI."""
        with self.session_factory() as session:
            result = session.execute(
                update(models.SessionWorkflow)
                .where(
                    models.SessionWorkflow.analysis_status == "error",
                    models.SessionWorkflow.lease_token.is_(None),
                    or_(
                        models.SessionWorkflow.analysis_error.startswith(
                            "Workflow analysis returned HTTP "
                        ),
                        models.SessionWorkflow.analysis_error
                        == "Workflow analysis rate or capacity limit (HTTP 429)",
                    ),
                )
                .values(
                    analysis_status="queued", analysis_error=None, analysis_attempts=0,
                    next_analysis_at=models.utc_now(),
                )
            )
            session.commit()
            return result.rowcount

    def view(self, branch_id: str) -> dict[str, Any]:
        with self.session_factory() as session:
            if session.get(models.ExecutionBranch, branch_id) is None:
                raise NotFoundError("Branch not found")
            row = session.get(models.SessionWorkflow, branch_id)
            graphs = list(row.graphs) if row else []
            for index, graph in enumerate(graphs):
                observed = graph.get("observed_at")
                expired = False
                if observed:
                    observed_time = datetime.fromisoformat(observed.replace("Z", "+00:00"))
                    expired = (
                        models.utc_now() - observed_time.replace(tzinfo=UTC)
                    ).total_seconds() > max(180, self.settings.octo_poll_seconds * 3)
                if graph["source"] == "octo" and (
                    expired or not self.octo.configured or self._octo_error
                ):
                    graphs[index] = {**graph, "stale": True}
            evidence = (row.evidence or {}).get("records", []) if row else []
            if row and row.analysis:
                current_ids = {item["id"] for item in evidence}
                evidence = [
                    *evidence,
                    *[
                        item
                        for item in row.analysis.get("evidence", [])
                        if item["id"] not in current_ids
                    ],
                ]
            stale = bool(row and row.analysis and row.input_hash != row.analyzed_hash)
            if row and row.analysis:
                graphs.append(self._analysis_graph(row, stale))
            return {
                "branch_id": branch_id,
                "enabled": self.settings.session_workflows_enabled,
                "observing": row is not None,
                "analysis_status": (
                    "disabled"
                    if not self.settings.session_workflow_analysis_enabled
                    else row.analysis_status
                    if row
                    else "not_started"
                ),
                "analysis_error": row.analysis_error if row else None,
                "analyzed_at": _iso(row.analyzed_at) if row else None,
                "observed_at": _iso(row.observed_at) if row else None,
                "next_analysis_at": _iso(row.next_analysis_at) if row else None,
                "analysis_stale": stale,
                "graphs": graphs,
                "evidence": [
                    {**item, "excerpt": _text(item.get("excerpt"), 1400)} for item in evidence
                ],
                "warnings": row.warnings if row else [],
                "octo": {
                    "state": "not_configured"
                    if not self.octo.configured
                    else (
                        "error"
                        if self._octo_error
                        else "connected"
                        if self._octo_synced_at
                        else "waiting"
                    ),
                    "last_synced_at": self._octo_synced_at,
                    "error": self._octo_error,
                },
            }

    async def refresh_all(self) -> None:
        if (
            self.octo.configured
            and time.monotonic() - self._last_octo_poll >= self.settings.octo_poll_seconds
        ):
            try:
                documents = await self.octo.workflows()
                await run_db(self._save_catalog, documents)
                self._octo_synced_at = models.utc_now().isoformat()
                self._octo_error = None
            except Exception as exc:
                self._octo_error = _text(exc, 500)
            finally:
                self._last_octo_poll = time.monotonic()
        for branch_id in await run_db(self._observed_branches):
            await self.refresh_one(branch_id)

    async def refresh_one(self, branch_id: str) -> None:
        try:
            await asyncio.to_thread(self.refresh_branch, branch_id)
        except Exception as exc:
            await run_db(self._refresh_error, branch_id, _text(exc, 500))

    def _observed_branches(self) -> list[str]:
        with self.session_factory() as session:
            return list(
                session.scalars(
                    select(models.SessionWorkflow.branch_id)
                    .join(
                        models.ExecutionBranch,
                        models.ExecutionBranch.id == models.SessionWorkflow.branch_id,
                    )
                    .where(models.ExecutionBranch.hidden.is_(False))
                )
            )

    def _save_catalog(self, documents: list[dict[str, Any]]) -> None:
        with self.session_factory() as session:
            now = models.utc_now()
            for document in documents:
                row = session.get(models.OctoWorkflowSnapshot, document["job_id"])
                if row is None:
                    row = models.OctoWorkflowSnapshot(
                        job_id=document["job_id"], document=document, observed_at=now
                    )
                    session.add(row)
                else:
                    row.document = document
                    row.observed_at = now
            # Reconcile deletions only after a complete, successful catalog read.
            session.execute(
                delete(models.OctoWorkflowSnapshot).where(
                    models.OctoWorkflowSnapshot.job_id.not_in(
                        [item["job_id"] for item in documents]
                    )
                )
            )
            session.commit()

    def _context(self, branch_id: str) -> dict[str, Any]:
        with self.session_factory() as session:
            branch = session.get(models.ExecutionBranch, branch_id)
            if not branch:
                raise NotFoundError("Branch not found")
            turns = list(
                session.scalars(
                    select(models.CodexTurn)
                    .where(
                        models.CodexTurn.branch_id == branch_id,
                        models.CodexTurn.status.in_(
                            ["completed", "interrupted", "error", "failed"]
                        ),
                    )
                    .order_by(models.CodexTurn.completed_at.desc(), models.CodexTurn.id)
                    .limit(40)
                )
            )
            thread_ids = set(
                session.scalars(
                    select(models.CodexThreadBinding.thread_id).where(
                        models.CodexThreadBinding.branch_id == branch_id
                    )
                )
            )
            if branch.codex_thread_id:
                thread_ids.add(branch.codex_thread_id)
            job_rows = session.execute(
                select(models.ExternalJob, models.Execution.branch_id)
                .outerjoin(models.Execution, models.ExternalJob.execution_id == models.Execution.id)
                .where(models.ExternalJob.work_item_id == branch.work_item_id)
                .order_by(models.ExternalJob.updated_at.desc())
                .limit(500)
            ).all()
            records = []
            remaining = self.settings.session_workflow_evidence_chars
            for turn in turns:
                if not (turn.prompt or turn.final_answer) or remaining < 100:
                    continue
                excerpt = _text(
                    f"Query: {turn.prompt or ''}\nFinal answer: {turn.final_answer}",
                    min(6000, remaining),
                )
                remaining -= len(excerpt)
                records.append(
                    {
                        "id": f"turn:{turn.id}",
                        "kind": "turn",
                        "label": _text(turn.prompt or "Recorded result", 120),
                        "excerpt": excerpt,
                        "at": _iso(turn.completed_at),
                        "status": turn.status,
                    }
                )
            records.reverse()
            references = "\n".join(f"{turn.prompt or ''}\n{turn.final_answer}" for turn in turns)
            jobs = []
            for job, execution_branch in job_rows:
                meta = job.metadata_json or {}
                matched = execution_branch == branch_id or _matches(
                    meta, branch_id, thread_ids, set()
                )
                # Never borrow another Branch's jobs simply because WorkItem matches.
                if not matched and execution_branch is None:
                    matched = any(
                        _exact_reference(str(value or ""), references)
                        for value in (
                            job.remote_job_id,
                            meta.get("job_id"),
                            meta.get("batch_task_id"),
                            meta.get("state_path"),
                        )
                    )
                if not matched:
                    continue
                jobs.append(
                    {
                        "id": job.id,
                        "remote_job_id": job.remote_job_id,
                        "provider": job.provider,
                        "status": job.status,
                        "metadata": meta,
                        "observed_at": _iso(job.last_observed_at),
                    }
                )
                records.append(
                    {
                        "id": f"job:{job.id}",
                        "kind": "job",
                        "label": _text(job.remote_job_id, 120),
                        "excerpt": _text(
                            f"{job.provider}: {job.remote_job_id}; status={job.status}"
                        ),
                        "at": _iso(job.last_observed_at),
                        "status": job.status,
                    }
                )
            snapshots = [
                (row.document, _iso(row.observed_at))
                for row in session.scalars(select(models.OctoWorkflowSnapshot))
            ]
            return {
                "branch_id": branch_id,
                "branch_name": branch.name,
                "thread_ids": thread_ids,
                "records": records,
                "jobs": jobs,
                "snapshots": snapshots,
                "references": references,
            }

    def refresh_branch(self, branch_id: str) -> None:
        context = self._context(branch_id)
        records = context["records"]
        job_ids = {value for job in context["jobs"] for value in (job["id"], job["remote_job_id"])}
        graphs: list[dict[str, Any]] = []
        warnings: list[str] = []
        for document, observed_at in context["snapshots"]:
            parent_match = _matches(document.get("meta"), branch_id, context["thread_ids"], job_ids)
            parent_match = parent_match or _exact_reference(
                document["job_id"], context["references"]
            )
            matched = {
                node["name"]
                for node in document["nodes"]
                if _matches(node.get("meta"), branch_id, context["thread_ids"], job_ids)
            }
            if not (parent_match or matched):
                continue
            nodes = []
            for raw in document["nodes"][:200]:
                ref = f"octo:{document['job_id']}:{raw['name']}"
                nodes.append(
                    _node(
                        raw["name"],
                        raw["status"],
                        [ref],
                        associated=parent_match or raw["name"] in matched,
                    )
                )
                records.append(
                    {
                        "id": ref,
                        "kind": "octo",
                        "label": _text(raw["name"], 120),
                        "excerpt": _text(f"Octo {document['job_id']}: {raw['status']}"),
                        "at": observed_at,
                        "status": raw["status"],
                    }
                )
            graphs.append(
                {
                    "id": f"octo:{document['job_id']}",
                    "title": _text(document["job_id"], 160),
                    "source": "octo",
                    "nodes": nodes,
                    "edges": document["edges"][:2000],
                    "observed_at": observed_at,
                    "summary": "",
                    "stale": bool(self._octo_error),
                    "job_id": document["job_id"],
                }
            )
        for job in context["jobs"]:
            if job["provider"] != "dag-runner":
                continue
            try:
                graph, dag_records = self._local_dag(job)
                graphs.append(graph)
                records.extend(dag_records)
            except (OSError, ValueError, TypeError) as exc:
                warnings.append(f"{job['remote_job_id']}: {_text(exc, 240)}")
        evidence = {
            "branch_id": branch_id,
            "branch_name": context["branch_name"],
            "records": records,
            "source_graphs": [
                {
                    "title": graph["title"],
                    "source": graph["source"],
                    "nodes": graph["nodes"],
                    "edges": graph["edges"],
                }
                for graph in graphs
            ],
        }
        analysis_evidence = bounded_evidence(
            evidence, self.settings.session_workflow_evidence_chars
        )
        records = analysis_evidence["records"]
        # Poll timestamps are not new evidence. Only changed records/topology trigger AI.
        fingerprint_input = {
            **analysis_evidence,
            "records": [
                {key: value for key, value in item.items() if key != "at"} for item in records
            ],
        }
        digest = hashlib.sha256(json.dumps(fingerprint_input, sort_keys=True).encode()).hexdigest()
        with self.session_factory() as session:
            row = session.get(models.SessionWorkflow, branch_id)
            if row is None:
                return
            changed = row.input_hash != digest
            row.graphs = graphs
            row.evidence = evidence
            row.observed_at = models.utc_now()
            row.warnings = warnings
            row.input_hash = digest
            if changed:
                row.analysis_attempts = 0
                if row.analysis_status != "running":
                    row.analysis_status = "queued" if records else "waiting"
                    row.analysis_error = None
                    cooldown = self.settings.session_workflow_analysis_cooldown_seconds
                    row.next_analysis_at = (
                        (row.analyzed_at + timedelta(seconds=cooldown))
                        if row.analyzed_at
                        else models.utc_now()
                    )
            session.commit()

    def _local_dag(self, job: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        def read(key: str) -> tuple[dict[str, Any], str]:
            raw = job["metadata"].get(key)
            if not raw or not Path(raw).is_absolute():
                raise ValueError(f"Missing absolute {key}")
            path = Path(raw).resolve()
            roots = [
                *self.settings.managed_job_allowed_roots,
                self.settings.workspace_root,
                self.settings.data_dir,
            ]
            if not any(path.is_relative_to(root.resolve()) for root in roots):
                raise ValueError(f"{key} is outside configured workflow roots")
            with path.open("rb") as handle:
                content = handle.read(self.settings.dag_state_max_bytes + 1)
            if len(content) > self.settings.dag_state_max_bytes:
                raise ValueError(f"{key} exceeds the size limit")
            value = json.loads(content)
            if not isinstance(value, dict):
                raise ValueError(f"Invalid {key}")
            return value, datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat()

        state, observed_at = read("state_path")
        definition, _ = read("dag_path")
        specs, states = definition.get("jobs", {}), state.get("jobs", {})
        if not isinstance(specs, dict) or not isinstance(states, dict):
            raise ValueError("Invalid DAG jobs")
        names = list(dict.fromkeys([*specs, *states]))[:200]
        nodes, edges, records = [], [], []
        for name in names:
            raw = states.get(name) or {}
            spec = specs.get(name) or {}
            status = str(raw.get("status", "pending"))
            ref = f"dag:{job['id']}:{name}"
            nodes.append(_node(name, status, [ref], detail=str(raw.get("error") or "")))
            dependencies = spec.get("depends_on", spec.get("needs", []))
            if isinstance(dependencies, str):
                dependencies = [dependencies]
            for source in dependencies if isinstance(dependencies, list) else []:
                if source in names:
                    edges.append({"source": source, "target": name})
            records.append(
                {
                    "id": ref,
                    "kind": "dag",
                    "label": _text(name, 120),
                    "excerpt": _text(f"{name}: {status}; job_id={raw.get('job_id', '')}"),
                    "at": observed_at,
                    "status": status,
                }
            )
        return {
            "id": f"dag:{job['id']}",
            "title": _text(job["remote_job_id"], 160),
            "source": "dag-runner",
            "nodes": nodes,
            "edges": edges,
            "observed_at": observed_at,
            "summary": "",
            "stale": False,
        }, records

    def _refresh_error(self, branch_id: str, error: str) -> None:
        with self.session_factory() as session:
            row = session.get(models.SessionWorkflow, branch_id)
            if row:
                row.warnings = [f"Workflow refresh failed: {error}"]
                row.graphs = [{**graph, "stale": True} for graph in row.graphs]
                session.commit()

    def _claim(self) -> dict[str, Any] | None:
        now = models.utc_now()
        with self.session_factory() as session:
            session.execute(
                update(models.SessionWorkflow)
                .where(
                    models.SessionWorkflow.analysis_status == "running",
                    models.SessionWorkflow.lease_until < now,
                    models.SessionWorkflow.analysis_attempts >= 3,
                )
                .values(
                    analysis_status="error",
                    analysis_error="Analysis worker lease expired",
                    lease_token=None,
                    lease_until=None,
                    next_analysis_at=None,
                )
            )
            session.commit()
            row = session.scalar(
                select(models.SessionWorkflow)
                .join(
                    models.ExecutionBranch,
                    models.ExecutionBranch.id == models.SessionWorkflow.branch_id,
                )
                .where(
                    models.ExecutionBranch.hidden.is_(False),
                    models.SessionWorkflow.analysis_status.in_(["queued", "error", "running"]),
                    models.SessionWorkflow.analysis_attempts < 3,
                    or_(
                        models.SessionWorkflow.lease_until.is_(None),
                        models.SessionWorkflow.lease_until < now,
                    ),
                    models.SessionWorkflow.next_analysis_at <= now,
                )
                .order_by(models.SessionWorkflow.next_analysis_at)
                .limit(1)
            )
            if row is None or not row.evidence.get("records"):
                return None
            token = str(uuid.uuid4())
            claimed = session.execute(
                update(models.SessionWorkflow)
                .where(
                    models.SessionWorkflow.branch_id == row.branch_id,
                    or_(
                        models.SessionWorkflow.lease_until.is_(None),
                        models.SessionWorkflow.lease_until < now,
                    ),
                )
                .values(
                    lease_token=token,
                    lease_until=now
                    + timedelta(
                        seconds=self.settings.session_workflow_analysis_timeout_seconds + 60
                    ),
                    analysis_status="running",
                    analysis_attempts=models.SessionWorkflow.analysis_attempts + 1,
                )
                .execution_options(synchronize_session=False)
            )
            if claimed.rowcount != 1:
                session.rollback()
                return None
            work = {
                "branch_id": row.branch_id,
                "token": token,
                "hash": row.input_hash,
                "evidence": bounded_evidence(
                    row.evidence, self.settings.session_workflow_evidence_chars
                ),
            }
            session.commit()
            return work

    async def analyze_next(self) -> bool:
        if not self.settings.session_workflow_analysis_enabled:
            return False
        work = await run_db(self._claim)
        if work is None:
            return False
        try:
            report = await asyncio.wait_for(
                self.analyst.analyze(work["evidence"]),
                self.settings.session_workflow_analysis_timeout_seconds,
            )
            validate_evidence(report, work["evidence"])
            await run_db(self._finish, work, report.model_dump(), None)
        except asyncio.CancelledError:
            await run_db(self._finish, work, None, "Analysis worker stopped; retry is queued")
            raise
        except Exception as exc:
            await run_db(
                self._finish, work, None, _text(exc, 1000) or "Workflow analysis timed out"
            )
        return True

    def _finish(
        self, work: dict[str, Any], report: dict[str, Any] | None, error: str | None
    ) -> None:
        with self.session_factory() as session:
            row = session.get(models.SessionWorkflow, work["branch_id"])
            if row is None or row.lease_token != work["token"]:
                return
            row.lease_token = None
            row.lease_until = None
            changed = row.input_hash != work["hash"]
            row.analysis_error = error
            now = models.utc_now()
            if report is not None:
                row.analysis = {**report, "evidence": work["evidence"]["records"]}
                row.analyzed_hash = work["hash"]
                row.analyzed_at = now
                row.analysis_status = "queued" if changed else "ready"
                row.next_analysis_at = now + timedelta(
                    seconds=self.settings.session_workflow_analysis_cooldown_seconds
                )
            else:
                row.analysis_status = "queued" if changed else "error"
                row.next_analysis_at = (
                    now + timedelta(seconds=min(900, 60 * 2**row.analysis_attempts))
                    if changed or row.analysis_attempts < 3
                    else None
                )
            session.commit()

    def _analysis_graph(self, row: models.SessionWorkflow, stale: bool) -> dict[str, Any]:
        records = {item["id"]: item for item in row.evidence.get("records", [])}
        nodes = []
        for raw in row.analysis.get("nodes", []):
            jobs = [records[ref] for ref in raw["job_refs"] if ref in records]
            # One-to-one job mappings can use observed status. A group of jobs
            # has no unambiguous aggregate status; keep the AI claim labelled.
            observed = jobs[0] if len(jobs) == 1 else None
            nodes.append(
                {
                    "id": raw["id"],
                    "title": raw["title"],
                    "detail": raw["detail"],
                    "status": normalized_status(observed["status"])
                    if observed
                    else raw["reported_status"],
                    "raw_status": observed["status"] if observed else raw["reported_status"],
                    "status_basis": "observed" if observed else "reported",
                    "evidence_refs": list(dict.fromkeys([*raw["evidence_refs"], *raw["job_refs"]])),
                    "associated": True,
                }
            )
        return {
            "id": "analysis",
            "title": "Session analysis",
            "source": "analysis",
            "nodes": nodes,
            "edges": row.analysis.get("edges", []),
            "summary": row.analysis.get("summary", ""),
            "observed_at": _iso(row.analyzed_at),
            "stale": stale,
        }

    async def reports(self, branch_id: str, workflow_id: str, node_id: str) -> list[dict[str, Any]]:
        view = await run_db(self.view, branch_id)
        graph = next(
            (
                item
                for item in view["graphs"]
                if item["id"] == workflow_id and item["source"] == "octo"
            ),
            None,
        )
        if graph is None or not any(item["id"] == node_id for item in graph["nodes"]):
            raise NotFoundError("No associated workflow node found")
        return await self.octo.reports(graph["job_id"], node_id)


def validate_evidence(report: WorkflowAnalysis, evidence: dict[str, Any]) -> None:
    known = {item["id"] for item in evidence["records"]}
    jobs = {item["id"] for item in evidence["records"] if item["kind"] == "job"}
    if report.summary and not report.evidence_refs:
        raise ValueError("Workflow summary is missing evidence")
    refs = set(report.evidence_refs)
    for node in report.nodes:
        refs.update(node.evidence_refs)
        if not set(node.job_refs).issubset(jobs):
            raise ValueError("Workflow analysis cites an unrelated job")
    for edge in report.edges:
        refs.update(edge.evidence_refs)
    if not refs.issubset(known):
        raise ValueError("Workflow analysis cites unknown evidence")


def bounded_evidence(evidence: dict[str, Any], max_chars: int) -> dict[str, Any]:
    # Keep recent conversational evidence and registered task facts in separate
    # budgets so a large task catalog cannot crowd out the user's actual request.
    records = evidence["records"]
    selected = []
    for is_turn in (True, False):
        budget = max_chars // 3
        candidates = [row for row in records if (row["kind"] == "turn") == is_turn]
        for row in reversed(candidates) if is_turn else candidates:
            size = len(json.dumps(row, ensure_ascii=False))
            if size <= budget:
                selected.append(row)
                budget -= size
    selected.reverse()
    known = {row["id"] for row in selected}
    graphs = []
    budget = max_chars // 3
    for graph in evidence["source_graphs"]:
        nodes = [node for node in graph["nodes"] if set(node["evidence_refs"]).issubset(known)]
        ids = {node["id"] for node in nodes}
        value = {
            **graph,
            "nodes": nodes,
            "edges": [
                edge for edge in graph["edges"] if edge["source"] in ids and edge["target"] in ids
            ],
        }
        size = len(json.dumps(value, ensure_ascii=False))
        if size <= budget:
            graphs.append(value)
            budget -= size
    return {
        **evidence,
        "records": selected,
        "source_graphs": graphs,
        "coverage": {
            "candidate_records": len(records),
            "included_records": len(selected),
            "window": "Latest 40 recorded turns and explicitly associated jobs",
        },
    }
