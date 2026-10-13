#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

DEFAULT_ENV = Path(os.environ.get("ROPS_ENV_FILE", ".env")).expanduser()
STATUSES = {"pending", "running", "succeeded", "failed"}
SECRET_KEY = re.compile(
    r"^(?:api[_-]?key|app[_-]?secret|password|authorization|access[_-]?token|token|secret)$", re.I
)


def guard_secrets(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if SECRET_KEY.fullmatch(key):
                raise ValueError("Credential fields cannot be published in a workflow")
            guard_secrets(item)
    elif isinstance(value, list):
        for item in value:
            guard_secrets(item)
    elif isinstance(value, str):
        if re.search(r"\b(?:wfk_|sk-)[A-Za-z0-9_-]{16,}|Bearer\s+\S+", value):
            raise ValueError("Credential-like text cannot be published in a workflow")
        if value.startswith(("https://", "http://")):
            url = urlsplit(value)
            params = [*parse_qsl(url.query), *parse_qsl(url.fragment)]
            if (
                url.username
                or url.password
                or any(
                    re.search(r"token|secret|signature|authorization|api[_-]?key", key, re.I)
                    for key, _ in params
                )
            ):
                raise ValueError("Signed or credential-bearing URLs cannot be published")


def name(value, *, limit=64, job=False):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError("Invalid workflow or node name")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Control characters are not allowed in names")
    if job and ("/" in value or "\\" in value):
        raise ValueError("Workflow IDs cannot contain slashes")
    return value


def validate(data):
    if not isinstance(data, dict) or set(data) - {"job_id", "nodes", "edges", "meta"}:
        raise ValueError("Expected job_id, nodes, edges, and optional meta")
    name(data.get("job_id"), limit=255, job=True)
    nodes = data.get("nodes")
    edges = data.get("edges", [])
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= 200:
        raise ValueError("Expected 1 to 200 nodes")
    if not isinstance(edges, list) or len(edges) > 2000:
        raise ValueError("Expected at most 2000 edges")
    normalized = []
    for node in nodes:
        if not isinstance(node, dict) or set(node) - {"name", "status", "meta"}:
            raise ValueError("Invalid node fields")
        node = {
            "name": name(node.get("name")),
            "status": node.get("status", "pending"),
            "meta": node.get("meta", {}),
        }
        if node["status"] not in STATUSES or not isinstance(node["meta"], dict):
            raise ValueError("Invalid node status or metadata")
        normalized.append(node)
    names = {node["name"] for node in normalized}
    if len(names) != len(nodes):
        raise ValueError("Duplicate node names")
    graph = TopologicalSorter({key: [] for key in names})
    for edge in edges:
        if not isinstance(edge, dict) or set(edge) != {"source", "target"}:
            raise ValueError("Each edge needs source and target")
        if edge["source"] not in names or edge["target"] not in names:
            raise ValueError("Unknown edge endpoint")
        graph.add(edge["target"], edge["source"])
    try:
        graph.prepare()
    except CycleError as exc:
        raise ValueError("Workflow dependencies contain a cycle") from exc
    meta = data.get("meta", {})
    if not isinstance(meta, dict):
        raise ValueError("Workflow metadata must be an object")
    result = {"job_id": data["job_id"], "nodes": normalized, "edges": edges, "meta": meta}
    guard_secrets(result)
    return result


def extension_definition(current, payload):
    if not isinstance(payload, dict) or set(payload) - {"nodes", "edges", "message"}:
        raise ValueError("Expected nodes, edges, and optional message for an extension")
    additions = payload.get("nodes", [])
    extra_edges = payload.get("edges", [])
    if not isinstance(additions, list) or len(additions) > 200:
        raise ValueError("Expected at most 200 additional nodes")
    if not isinstance(extra_edges, list) or len(extra_edges) > 2000:
        raise ValueError("Expected at most 2000 additional edges")
    if "message" in payload and (
        not isinstance(payload["message"], str) or len(payload["message"]) > 10000
    ):
        raise ValueError("Extension message must be a string of at most 10000 characters")
    guard_secrets(payload)
    nodes = [{"name": node["name"], "meta": node.get("meta", {})} for node in current["nodes"]]
    existing = {node["name"]: node for node in nodes}
    seen = set()
    for node in additions:
        if not isinstance(node, dict) or set(node) - {"name", "meta"}:
            raise ValueError("Extension nodes accept name and meta only; use update for status")
        node_name = name(node.get("name"))
        if node_name in seen:
            raise ValueError("Duplicate node names in extension")
        seen.add(node_name)
        meta = node.get("meta", {})
        if not isinstance(meta, dict):
            raise ValueError("Invalid node metadata")
        if node_name in existing:
            if "meta" in node and meta != existing[node_name]["meta"]:
                raise ValueError(
                    "Node already exists with different metadata; use update explicitly"
                )
        else:
            nodes.append({"name": node_name, "meta": meta})
    edges = list(current["edges"])
    edge_keys = {(edge["source"], edge["target"]) for edge in edges}
    for edge in extra_edges:
        if not isinstance(edge, dict) or set(edge) != {"source", "target"}:
            raise ValueError("Each edge needs source and target")
        key = (name(edge["source"]), name(edge["target"]))
        if key not in edge_keys:
            edges.append(edge)
            edge_keys.add(key)
    # Validate the entire merged graph, including references to existing nodes.
    validate({"job_id": current["job_id"], "nodes": nodes, "edges": edges})
    version = current.get("updated_at")
    if not isinstance(version, str) or not version:
        raise ValueError("Workflow has no updated_at; cannot safely extend it")
    definition = {"nodes": nodes, "edges": edges, "expected_updated_at": version}
    if "current_stage" in current:
        definition["current_stage"] = current["current_stage"]
    if "message" in payload:
        definition["message"] = payload["message"]
    return definition


class ApiError(RuntimeError):
    def __init__(self, status):
        self.status = status
        super().__init__(f"Octo returned HTTP {status}; no automatic retry was made")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self, base, key):
        parts = urlsplit(base)
        if parts.scheme not in {"http", "https"} or not parts.netloc or not key:
            raise ValueError("Configure OCTO_BASE_URL and OCTO_API_KEY (or ROPS_ equivalents)")
        if parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("Octo base URL cannot contain credentials, query, or fragment")
        self.base, self.key = base.rstrip("/"), key
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def request(self, method, path, payload=None):
        body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        if body and self.key.encode() in body:
            raise ValueError("The API key cannot be published in workflow content")
        request = Request(
            self.base + "/api/v1" + path,
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with self.opener.open(request, timeout=20) as response:
                raw = response.read(2_000_001)
        except HTTPError as exc:
            raise ApiError(exc.code) from None
        except (URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(
                "Octo connection failed; inspect the same ID before retrying"
            ) from exc
        if len(raw) > 2_000_000:
            raise ValueError("Octo response exceeds the size limit")
        return json.loads(raw)

    def show(self, job_id):
        name(job_id, limit=255, job=True)
        return self.request("GET", "/workflows/" + quote(job_id, safe=""))

    def create(self, payload):
        payload = validate(payload)
        try:
            existing = self.show(payload["job_id"])
        except ApiError as exc:
            if exc.status != 404:
                raise
            return self.request("POST", "/workflows", payload), "created"
        actual = validate(
            {
                **{key: existing[key] for key in ("job_id", "edges", "meta")},
                "nodes": [
                    {key: node[key] for key in ("name", "status", "meta")}
                    for node in existing["nodes"]
                ],
            }
        )
        for document in (actual, payload):
            document["nodes"].sort(key=lambda node: node["name"])
            document["edges"].sort(key=lambda edge: (edge["source"], edge["target"]))
        if actual != payload:
            raise ValueError(
                "Workflow already exists with different content; inspect before updating"
            )
        return existing, "unchanged"

    def update(self, job_id, payload):
        if not isinstance(payload, dict) or set(payload) - {
            "current_stage",
            "node_updates",
            "meta",
            "message",
        }:
            raise ValueError("Invalid status update fields")
        guard_secrets(payload)
        current = self.show(job_id)
        names = {node["name"] for node in current["nodes"]}
        if payload.get("current_stage") not in names:
            raise ValueError("current_stage must name an existing node")
        updates = payload.get("node_updates", [])
        if not isinstance(updates, list) or len(updates) > 200:
            raise ValueError("Expected at most 200 node updates")
        seen = set()
        for update in updates:
            if not isinstance(update, dict) or set(update) - {"name", "status", "meta"}:
                raise ValueError("Invalid node update fields")
            if update.get("name") not in names or update["name"] in seen:
                raise ValueError("Unknown or duplicate node update")
            if "status" in update and update["status"] not in STATUSES:
                raise ValueError("Invalid node status")
            seen.add(update["name"])
        self.request(
            "PUT",
            f"/workflows/{quote(job_id, safe='')}/advance",
            {
                **payload,
                "expected_updated_at": current["updated_at"],
            },
        )
        return self.show(job_id), "updated"

    def extend(self, job_id, payload, *, dry_run=False):
        current = self.show(job_id)
        definition = extension_definition(current, payload)
        if len(definition["nodes"]) == len(current["nodes"]) and len(definition["edges"]) == len(
            current["edges"]
        ):
            return current, "unchanged"
        if dry_run:
            existing = {node["name"]: node for node in current["nodes"]}
            return {
                **current,
                "nodes": [
                    existing.get(node["name"], {**node, "status": "pending"})
                    for node in definition["nodes"]
                ],
                "edges": definition["edges"],
            }, "planned"
        self.request("PUT", f"/workflows/{quote(job_id, safe='')}/definition", definition)
        return self.show(job_id), "extended"


def load_file(path):
    with Path(path).open("rb") as stream:
        raw = stream.read(262145)
    if len(raw) > 262144:
        raise ValueError("Workflow JSON must be at most 256 KiB")
    return json.loads(raw)


def load_config(env_file=None):
    if env_file is None:
        config_home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        user_env = config_home / "research-ops" / "octo.env"
        env_file = user_env if user_env.is_file() else DEFAULT_ENV
    config = {}
    try:
        if env_file.is_file():
            from dotenv import dotenv_values

            config = dotenv_values(env_file)
    except PermissionError:
        raise ValueError(
            "Octo configuration is not readable; use --env-file with an Octo-only "
            "credential file, or provision ~/.config/research-ops/octo.env"
        ) from None
    config.update(os.environ)
    return config


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Publish workflow visualizations, never execute jobs"
    )
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--base-url")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("validate", "create", "show", "update", "extend"):
        child = sub.add_parser(command)
        if command in {"validate", "create", "update", "extend"}:
            child.add_argument("--file", required=True)
        if command in {"show", "update", "extend"}:
            child.add_argument("--job-id", required=True)
        if command == "extend":
            child.add_argument(
                "--dry-run", action="store_true", help="Read and validate without writing"
            )
    args = parser.parse_args(argv)
    payload = load_file(args.file) if hasattr(args, "file") else None
    if args.command == "validate":
        result = validate(payload)
        print(json.dumps({"valid": True, "job_id": result["job_id"]}))
        return 0
    config = load_config(args.env_file)
    client = Client(
        args.base_url
        or config.get("OCTO_BASE_URL")
        or config.get("ROPS_OCTO_BASE_URL")
        or "http://198.51.100.139:8300",
        config.get("OCTO_API_KEY") or config.get("ROPS_OCTO_API_KEY"),
    )
    if args.command == "create":
        payload = validate(payload)
        thread = os.environ.get("CODEX_THREAD_ID")
        if thread:
            payload["meta"].setdefault("codex_session_id", thread)
        result, action = client.create(payload)
    elif args.command == "update":
        result, action = client.update(args.job_id, payload)
    elif args.command == "extend":
        result, action = client.extend(args.job_id, payload, dry_run=args.dry_run)
    else:
        result, action = client.show(args.job_id), "read"
    url = f"{client.base}/#workflow/{quote(result['job_id'], safe='')}"
    title = str(result.get("meta", {}).get("title") or result["job_id"])
    title = re.sub(r"[\[\]\\\r\n]", " ", title)[:200]
    print(
        json.dumps(
            {
                "action": action,
                "job_id": result["job_id"],
                "url": url,
                "status": result.get("status"),
                "nodes": [
                    {"name": node["name"], "status": node["status"]}
                    for node in result.get("nodes", [])
                ],
                **({"edges": result["edges"]} if args.command == "extend" else {}),
            },
            ensure_ascii=False,
        )
    )
    print(f"[Workflow: {title}]({url})")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError, KeyError, TypeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
