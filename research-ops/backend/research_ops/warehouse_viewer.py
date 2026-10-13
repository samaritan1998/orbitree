from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from research_ops.services.warehouse_trajectories import (
    DEFAULT_WAREHOUSE_ROOT,
    WarehouseTrajectoryError,
    WarehouseTrajectoryService,
)


class SampleRequest(BaseModel):
    dataset: str | None = Field(default=None, max_length=500)
    batch: str | None = Field(default=None, max_length=500)
    per_batch: int = Field(default=10, ge=1, le=20)
    seed: int = Field(default=42, ge=0, le=2_147_483_647)


def create_app(
    *,
    warehouse_root: Path | None = None,
    static_root: Path | None = None,
) -> FastAPI:
    warehouse = warehouse_root or Path(
        os.environ.get("WAREHOUSE_ROOT", str(DEFAULT_WAREHOUSE_ROOT))
    )
    static = static_root or Path(
        os.environ.get(
            "WAREHOUSE_VIEWER_STATIC_ROOT",
            Path(__file__).resolve().parents[2] / "web" / "warehouse-viewer",
        )
    )
    service = WarehouseTrajectoryService(warehouse)
    app = FastAPI(title="Warehouse Trajectory Review", version="1.0.0")
    app.state.trajectory_service = service

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "warehouse_root": str(service.warehouse_root)}

    @app.post("/api/samples")
    async def sample_source(payload: SampleRequest) -> dict:
        try:
            sources = [
                (kind, value.strip())
                for kind, value in (("dataset", payload.dataset), ("batch", payload.batch))
                if value and value.strip()
            ]
            if len(sources) != 1:
                raise WarehouseTrajectoryError("dataset 和 batch 必须且只能提供一个")
            kind, value = sources[0]
            sampler = service.sample_dataset if kind == "dataset" else service.sample_batch
            return sampler(value, per_batch=payload.per_batch, seed=payload.seed)
        except WarehouseTrajectoryError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/sessions/{session_id}/samples/{sample_id}")
    async def get_sample(session_id: str, sample_id: str) -> dict:
        try:
            return service.get_sample(session_id, sample_id)
        except WarehouseTrajectoryError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    if static.is_dir():
        app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        path = static / "index.html"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Viewer assets are unavailable")
        return FileResponse(path, headers={"Cache-Control": "no-store"})

    return app


app = create_app()
