FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock README.md ./
COPY backend ./backend
COPY scripts/run_openreward_ppt_runtime.py ./scripts/run_openreward_ppt_runtime.py

RUN uv sync --frozen --no-dev --extra ppt-runtime

ENV ORS_PPT_REQUIRE_AUTH=true

ENTRYPOINT ["/app/.venv/bin/python", "scripts/run_openreward_ppt_runtime.py"]
CMD ["--host", "0.0.0.0", "--port", "9103"]

