FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock README.md ./
COPY backend ./backend
COPY scripts/run_openreward_swe_runtime.py ./scripts/run_openreward_swe_runtime.py

RUN uv sync --frozen --no-dev --extra swe-runtime
# The official package declares dataset collection and Modal as hard
# dependencies even though this sidecar loads only harness code. Keep the
# evaluator version pinned without installing that unrelated dependency tree.
RUN uv pip install --python /app/.venv/bin/python --no-deps swebench==4.0.4

ENV ORS_SWE_REQUIRE_AUTH=true

ENTRYPOINT ["/app/.venv/bin/python", "scripts/run_openreward_swe_runtime.py"]
CMD ["--host", "0.0.0.0", "--port", "9101"]
