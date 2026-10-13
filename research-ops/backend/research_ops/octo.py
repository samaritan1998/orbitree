from __future__ import annotations

import json
from graphlib import CycleError, TopologicalSorter
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import BaseModel, Field, ValidationError, model_validator

from research_ops.settings import Settings


class OctoNode(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    status: str = Field(min_length=1, max_length=40)
    meta: dict[str, Any] = Field(default_factory=dict)


class OctoEdge(BaseModel):
    source: str
    target: str


class OctoWorkflow(BaseModel):
    job_id: str = Field(min_length=1, max_length=255)
    meta: dict[str, Any] = Field(default_factory=dict)
    nodes: list[OctoNode] = Field(max_length=200)
    edges: list[OctoEdge] = Field(max_length=2000)
    status: str
    current_stage: str | None = None
    created_at: str | None = None
    updated_at: str | None = None

    @model_validator(mode="after")
    def valid_topology(self) -> OctoWorkflow:
        names = {node.name for node in self.nodes}
        if len(names) != len(self.nodes):
            raise ValueError("Duplicate Octo node names")
        graph = TopologicalSorter()
        for name in names:
            graph.add(name)
        for edge in self.edges:
            if edge.source not in names or edge.target not in names:
                raise ValueError("Unknown Octo edge endpoint")
            graph.add(edge.target, edge.source)
        try:
            graph.prepare()
        except CycleError as exc:
            raise ValueError("Octo topology contains a cycle") from exc
        return self


class OctoClient:
    """Read-only adapter. No workflow mutation or report upload methods."""

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.transport = transport

    @property
    def configured(self) -> bool:
        return bool(self.settings.octo_api_key)

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        if not self.configured:
            raise RuntimeError("Octo API key is not configured")
        async with (
            httpx.AsyncClient(
                base_url=self.settings.octo_base_url.rstrip("/"),
                headers={"Authorization": f"Bearer {self.settings.octo_api_key}"},
                timeout=self.settings.octo_timeout_seconds,
                trust_env=False,
                follow_redirects=False,
                transport=self.transport,
            ) as client,
            client.stream("GET", path, params=params) as response,
        ):
            if response.status_code != 200:
                raise RuntimeError(f"Octo returned HTTP {response.status_code}")
            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > 8 * 1024 * 1024:
                    raise RuntimeError("Octo response exceeds the size limit")
        return json.loads(content)

    async def workflows(self) -> list[dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        maximum = self.settings.octo_max_workflows
        expected_total: int | None = None
        for offset in range(0, maximum + 1, 100):
            payload = await self._get("/api/v1/workflows", {"limit": 100, "offset": offset})
            items = payload.get("workflows") if isinstance(payload, dict) else None
            if not isinstance(items, list):
                raise RuntimeError("Octo returned an invalid workflow list")
            total = payload.get("total")
            if not isinstance(total, int) or total < 0:
                raise RuntimeError("Octo returned an invalid catalog count")
            if expected_total is not None and total != expected_total:
                raise RuntimeError("Octo catalog changed during pagination; retaining cache")
            expected_total = total
            for item in items:
                try:
                    item = OctoWorkflow.model_validate(item).model_dump()
                except ValidationError as exc:
                    raise RuntimeError("Octo returned an invalid workflow schema") from exc
                result[item["job_id"]] = item
            if len(result) > maximum:
                raise RuntimeError("Octo catalog limit reached; increase ROPS_OCTO_MAX_WORKFLOWS")
            if len(items) < 100:
                if len(result) != expected_total:
                    raise RuntimeError("Octo returned an incomplete catalog; retaining cache")
                return list(result.values())
        raise RuntimeError("Octo catalog pagination did not finish")

    async def reports(self, job_id: str, node_name: str) -> list[dict[str, Any]]:
        path = f"/api/v1/workflows/{quote(job_id, safe='')}/reports"
        payload = await self._get(path, {"node_name": node_name, "latest": "true", "limit": 2})
        items = payload.get("reports") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise RuntimeError("Octo returned an invalid report list")
        reports = []
        for item in items[:2]:
            report = await self._get(f"{path}/{quote(str(item['id']), safe='')}", {})
            if not isinstance(report, dict) or report.get("node_name") != node_name:
                raise RuntimeError("Octo report node does not match")
            reports.append(report)
        return reports
