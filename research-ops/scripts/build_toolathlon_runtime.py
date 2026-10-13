#!/usr/bin/env python3
"""Build the pinned official Toolathlon-Gym ORS image reproducibly."""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

PINNED_REVISION = "9c82859d05bc1f0376150d749cd2e3d43471d583"
EXPECTED_FROM = "FROM ubuntu:22.04"
UPSTREAM_UV_BLOCK = """# uv
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH=\"/root/.local/bin:$PATH\"
"""
PINNED_UV_BLOCK = """# uv (installed into the Python base image)
RUN pip install --no-cache-dir uv==0.11.29
"""
NODE_ARCHIVE = "node-v22.17.0-linux-x64.tar.gz"
NODE_SHA256 = "0fa01328a0f3d10800623f7107fbcd654a60ec178fab1ef5b9779e94e0419e1a"
TASK_DATABASE_PATCH = Path(__file__).with_name("patch_toolathlon_task_databases.py")
CANVAS_HTTP_PATCH = Path(__file__).with_name("patch_toolathlon_canvas_http.py")
GOOGLE_FORMS_BUILD_FIX = "explicit-npm-build-v1"
CANVAS_PG_DEPENDENCIES = "pg-types-pg-v1"
UPSTREAM_NODE_BLOCK = """# Node.js 22
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \\
    && apt-get install -y nodejs \\
    && rm -rf /var/lib/apt/lists/* \\
    && npm install -g npm@10
"""
PINNED_NODE_BLOCK = """# Node.js 22 (verified upstream binary; avoids NodeSource mirror failures)
COPY .research-ops-build/node-v22.17.0-linux-x64.tar.gz /tmp/node.tar.gz
RUN echo \\
        \"0fa01328a0f3d10800623f7107fbcd654a60ec178fab1ef5b9779e94e0419e1a  /tmp/node.tar.gz\" \\
        | sha256sum -c - \\
    && tar -xzf /tmp/node.tar.gz -C /usr/local --strip-components=1 \\
    && rm /tmp/node.tar.gz \\
    && node --version \\
    && npm install -g npm@10
"""
UPSTREAM_LOCAL_SERVERS_BLOCK = """# Copy and build MCP servers
COPY local_servers/ /opt/local_servers/
"""
PATCHED_LOCAL_SERVERS_BLOCK = """# Copy and build MCP servers
COPY local_servers/ /opt/local_servers/
COPY .research-ops-build/patch_toolathlon_canvas_http.py /tmp/patch_toolathlon_canvas_http.py
RUN python3 /tmp/patch_toolathlon_canvas_http.py \\
        --client /opt/local_servers/mcp-canvas-lms/src/client.ts \\
    && cd /opt/local_servers/mcp-canvas-lms \\
    && npm install pg @types/pg
"""
UPSTREAM_CONFIG_BLOCK = """COPY configs/ /app/configs/
"""
PATCHED_CONFIG_BLOCK = """COPY configs/ /app/configs/
RUN python3 /tmp/patch_toolathlon_canvas_http.py \\
        --config /app/configs/mcp_servers/canvas.yaml \\
    && rm /tmp/patch_toolathlon_canvas_http.py
"""
UPSTREAM_ENTRYPOINT_BLOCK = """COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh
"""
PATCHED_ENTRYPOINT_BLOCK = """COPY entrypoint.sh /app/entrypoint.sh
COPY .research-ops-build/patch_toolathlon_task_databases.py /tmp/patch_toolathlon_task_databases.py
RUN python3 /tmp/patch_toolathlon_task_databases.py /app/tasks/finalpool \\
    && rm /tmp/patch_toolathlon_task_databases.py \\
    && chmod +x /app/entrypoint.sh
"""
UPSTREAM_YAHOO_FINANCE_BLOCK = """# yahoo-finance Toolathlon fork uses psycopg2 for PG-backed data
RUN cd /opt/local_servers/yahoo-finance-mcp && uv add psycopg2-binary
"""
PATCHED_YAHOO_FINANCE_BLOCK = """# yahoo-finance Toolathlon fork uses psycopg2 for PG-backed data
RUN cd /opt/local_servers/yahoo-finance-mcp && uv add psycopg2-binary

# The upstream parallel Node build can hide an individual background failure.
RUN cd /opt/local_servers/google-forms-mcp \\
    && npm install \\
    && npm run build \\
    && test -x build/index.js
"""


def _git(source: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(source), *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed")
    return result.stdout.strip()


def _node_archive(source: Path) -> Path:
    directory = source / ".research-ops-build"
    directory.mkdir(exist_ok=True)
    destination = directory / NODE_ARCHIVE
    if destination.is_file():
        digest = hashlib.sha256(destination.read_bytes()).hexdigest()
        if digest == NODE_SHA256:
            return destination
    partial = destination.with_suffix(destination.suffix + ".partial")
    partial.unlink(missing_ok=True)
    result = subprocess.run(
        [
            "curl",
            "--retry",
            "5",
            "--retry-all-errors",
            "-fsSL",
            "-o",
            str(partial),
            f"https://nodejs.org/dist/v22.17.0/{NODE_ARCHIVE}",
        ],
        check=False,
        timeout=300,
    )
    if result.returncode != 0 or not partial.is_file():
        raise SystemExit("cannot download the pinned Node.js build input")
    digest = hashlib.sha256(partial.read_bytes()).hexdigest()
    if digest != NODE_SHA256:
        partial.unlink(missing_ok=True)
        raise SystemExit("downloaded Node.js build input failed SHA256 verification")
    partial.replace(destination)
    return destination


def _task_database_patch(source: Path) -> str:
    if not TASK_DATABASE_PATCH.is_file():
        raise SystemExit("Toolathlon task database compatibility patch is missing")
    raw = TASK_DATABASE_PATCH.read_bytes()
    destination = source / ".research-ops-build" / TASK_DATABASE_PATCH.name
    destination.parent.mkdir(exist_ok=True)
    if not destination.is_file() or destination.read_bytes() != raw:
        shutil.copyfile(TASK_DATABASE_PATCH, destination)
    return hashlib.sha256(raw).hexdigest()


def _canvas_http_patch(source: Path) -> str:
    if not CANVAS_HTTP_PATCH.is_file():
        raise SystemExit("Toolathlon Canvas HTTP compatibility patch is missing")
    raw = CANVAS_HTTP_PATCH.read_bytes()
    destination = source / ".research-ops-build" / CANVAS_HTTP_PATCH.name
    destination.parent.mkdir(exist_ok=True)
    if not destination.is_file() or destination.read_bytes() != raw:
        shutil.copyfile(CANVAS_HTTP_PATCH, destination)
    return hashlib.sha256(raw).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument(
        "--image",
        default=f"research-ops/toolathlon-gym:{PINNED_REVISION[:8]}",
    )
    parser.add_argument(
        "--base-image",
        default="python:3.12-slim",
        help="Local/internal Debian-compatible base; avoids an unavailable Docker Hub mirror.",
    )
    parser.add_argument("--allow-dirty", action="store_true")
    args = parser.parse_args()

    source = args.source.expanduser().resolve()
    if _git(source, "rev-parse", "HEAD") != PINNED_REVISION:
        raise SystemExit("Toolathlon source revision does not match the pinned ORS commit")
    if not args.allow_dirty and _git(source, "status", "--porcelain", "--untracked-files=no"):
        raise SystemExit("Toolathlon source has tracked modifications")
    _node_archive(source)
    task_database_patch_sha256 = _task_database_patch(source)
    canvas_http_patch_sha256 = _canvas_http_patch(source)
    dockerfile = (source / "Dockerfile").read_text(encoding="utf-8")
    first, separator, remainder = dockerfile.partition("\n")
    if first.strip() != EXPECTED_FROM or not separator:
        raise SystemExit("Toolathlon Dockerfile base no longer matches the reviewed revision")
    generated = (
        "ARG TOOLATHLON_BASE_IMAGE\n"
        "FROM ${TOOLATHLON_BASE_IMAGE}\n"
        f'LABEL org.opencontainers.image.revision="{PINNED_REVISION}"\n'
        + remainder
    )
    if UPSTREAM_NODE_BLOCK not in generated:
        raise SystemExit("Toolathlon Node.js build block no longer matches the reviewed revision")
    if UPSTREAM_UV_BLOCK not in generated:
        raise SystemExit("Toolathlon uv build block no longer matches the reviewed revision")
    if UPSTREAM_ENTRYPOINT_BLOCK not in generated:
        raise SystemExit("Toolathlon entrypoint block no longer matches the reviewed revision")
    if UPSTREAM_LOCAL_SERVERS_BLOCK not in generated:
        raise SystemExit("Toolathlon local server block no longer matches the reviewed revision")
    if UPSTREAM_CONFIG_BLOCK not in generated:
        raise SystemExit("Toolathlon config block no longer matches the reviewed revision")
    if UPSTREAM_YAHOO_FINANCE_BLOCK not in generated:
        raise SystemExit("Toolathlon Yahoo Finance block no longer matches the reviewed revision")
    generated = generated.replace(UPSTREAM_UV_BLOCK, PINNED_UV_BLOCK)
    generated = generated.replace(UPSTREAM_NODE_BLOCK, PINNED_NODE_BLOCK)
    generated = generated.replace(
        UPSTREAM_LOCAL_SERVERS_BLOCK,
        PATCHED_LOCAL_SERVERS_BLOCK,
    )
    generated = generated.replace(UPSTREAM_CONFIG_BLOCK, PATCHED_CONFIG_BLOCK)
    generated = generated.replace(UPSTREAM_ENTRYPOINT_BLOCK, PATCHED_ENTRYPOINT_BLOCK)
    generated = generated.replace(
        UPSTREAM_YAHOO_FINANCE_BLOCK,
        PATCHED_YAHOO_FINANCE_BLOCK,
    )
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=".Dockerfile",
    ) as handle:
        handle.write(generated)
        handle.flush()
        command = [
            "docker",
            "build",
            "--build-arg",
            f"TOOLATHLON_BASE_IMAGE={args.base_image}",
            "--label",
            f"research-ops.upstream-revision={PINNED_REVISION}",
            "--label",
            f"research-ops.task-db-patch-sha256={task_database_patch_sha256}",
            "--label",
            f"research-ops.google-forms-build-fix={GOOGLE_FORMS_BUILD_FIX}",
            "--label",
            f"research-ops.canvas-http-patch-sha256={canvas_http_patch_sha256}",
            "--label",
            f"research-ops.canvas-pg-dependencies={CANVAS_PG_DEPENDENCIES}",
            "--tag",
            args.image,
            "--file",
            handle.name,
            str(source),
        ]
        proxy_args: list[str] = []
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"):
            value = str(os.environ.get(name, "")).strip()
            if value:
                proxy_args.extend(["--build-arg", f"{name}={value}"])
        command[2:2] = proxy_args
        result = subprocess.run(command, check=False)
    if result.returncode != 0:
        raise SystemExit(result.returncode)
    inspected = subprocess.run(
        [
            "docker",
            "image",
            "inspect",
            args.image,
            "--format",
            "{{index .Config.Labels \"research-ops.upstream-revision\"}} "
            "{{index .Config.Labels \"research-ops.task-db-patch-sha256\"}} "
            "{{index .Config.Labels \"research-ops.google-forms-build-fix\"}} "
            "{{index .Config.Labels \"research-ops.canvas-http-patch-sha256\"}} "
            "{{index .Config.Labels \"research-ops.canvas-pg-dependencies\"}}",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    expected_labels = (
        f"{PINNED_REVISION} {task_database_patch_sha256} {GOOGLE_FORMS_BUILD_FIX} "
        f"{canvas_http_patch_sha256} {CANVAS_PG_DEPENDENCIES}"
    )
    if inspected.returncode != 0 or inspected.stdout.strip() != expected_labels:
        raise SystemExit("built Toolathlon image is missing provenance labels")
    print(
        f"built {args.image} from {PINNED_REVISION} "
        f"with task DB patch {task_database_patch_sha256} "
        f"Google Forms fix {GOOGLE_FORMS_BUILD_FIX}, and Canvas HTTP patch "
        f"{canvas_http_patch_sha256} with {CANVAS_PG_DEPENDENCIES}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
