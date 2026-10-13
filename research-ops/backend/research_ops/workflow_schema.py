from __future__ import annotations

from graphlib import CycleError, TopologicalSorter
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

WorkflowStatus = Literal[
    "pending", "running", "completed", "failed", "blocked", "cancelled", "unknown"
]


class AnalysisNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=120)
    detail: str = Field(max_length=1200)
    reported_status: WorkflowStatus
    evidence_refs: list[str] = Field(min_length=1, max_length=12)
    job_refs: list[str] = Field(max_length=12)


class AnalysisEdge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    target: str
    evidence_refs: list[str] = Field(min_length=1, max_length=12)


class WorkflowAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(max_length=1600)
    evidence_refs: list[str] = Field(max_length=20)
    nodes: list[AnalysisNode] = Field(max_length=24)
    edges: list[AnalysisEdge] = Field(max_length=64)

    @model_validator(mode="after")
    def valid_dag(self) -> WorkflowAnalysis:
        ids = {node.id for node in self.nodes}
        if len(ids) != len(self.nodes):
            raise ValueError("Workflow node IDs must be unique")
        graph = TopologicalSorter()
        for node in ids:
            graph.add(node)
        seen: set[tuple[str, str]] = set()
        for edge in self.edges:
            if edge.source not in ids or edge.target not in ids:
                raise ValueError("Workflow edge refers to an unknown node")
            if (edge.source, edge.target) in seen:
                raise ValueError("Duplicate workflow edge")
            seen.add((edge.source, edge.target))
            graph.add(edge.target, edge.source)
        try:
            graph.prepare()
        except CycleError as exc:
            raise ValueError("Workflow contains a dependency cycle") from exc
        return self


def normalized_status(raw: str) -> WorkflowStatus:
    status = raw.strip().lower()
    return {
        "created": "pending",
        "queued": "pending",
        "submitted": "pending",
        "pending": "pending",
        "running": "running",
        "succeeded": "completed",
        "success": "completed",
        "completed": "completed",
        "failed": "failed",
        "error": "failed",
        "blocked": "blocked",
        "cancelled": "cancelled",
        "canceled": "cancelled",
    }.get(status, "unknown")  # type: ignore[return-value]
