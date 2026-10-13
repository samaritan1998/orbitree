ARG SLIDEFORGE_REWARD_IMAGE=registry.example.com/acg-agi/ppt-rollout:0610-deps
FROM ${SLIDEFORGE_REWARD_IMAGE}

USER root
WORKDIR /research-ops

COPY backend ./backend
COPY scripts/run_slideforge_reward_adapter.py ./scripts/run_slideforge_reward_adapter.py

RUN python3 -m pip install --no-cache-dir \
        "fastapi>=0.116,<1" \
        "uvicorn>=0.35,<1"

# The production dependency image supplies the office/runtime toolchain.  The
# SlideForgePipeline source is deliberately mounted read-only at deployment
# time, so fail the build early if a replacement base image lacks the tools
# needed by its reward workflow.
RUN command -v claude >/dev/null \
    && command -v ccrelay >/dev/null \
    && command -v soffice >/dev/null \
    && command -v pdftoppm >/dev/null \
    && test -d /opt/claude-skills/pptx

ENV PYTHONPATH=/research-ops/backend \
    ORS_SLIDEFORGE_REWARD_REQUIRE_AUTH=true

ENTRYPOINT ["python3", "scripts/run_slideforge_reward_adapter.py"]
CMD ["--host", "0.0.0.0", "--port", "9104"]
