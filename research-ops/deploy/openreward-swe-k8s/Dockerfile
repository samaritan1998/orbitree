FROM python:3.12-slim

ENV PIP_INDEX_URL="https://packages.example.com/simple/"
ENV PIP_NO_CACHE_DIR="1"

# Source code is mounted from CFS by the K8s Deployment.  This image only
# carries the API stack, Kubernetes client, and pinned SWE verifier packages.
RUN pip install \
      "fastapi>=0.116,<1" \
      "httpx>=0.28,<1" \
      "pydantic-settings>=2.8,<3" \
      "python-dotenv>=1,<2" \
      "pyyaml>=6,<7" \
      "uvicorn>=0.35,<1" \
      "kubernetes==36.0.3" \
      "unidiff==0.7.5" \
    && pip install --no-deps "swebench==4.0.4"

ENV PYTHONPATH="/opt/research-ops/backend"
ENV PYTHONDONTWRITEBYTECODE="1"
ENV ORS_SWE_ENGINE="kubernetes"
ENV ORS_SWE_REQUIRE_AUTH="true"

ENTRYPOINT ["python3", "/opt/research-ops/scripts/run_openreward_swe_runtime.py"]
CMD ["--host", "0.0.0.0", "--port", "9101"]
