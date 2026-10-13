from pathlib import Path

import pytest

from scripts.patch_toolathlon_canvas_http import patch_client, patch_config


def test_canvas_patch_allows_explicit_http_origin(tmp_path: Path) -> None:
    client = tmp_path / "client.ts"
    client.write_text(
        "import axios, { AxiosInstance, AxiosError } from 'axios';\n\n"
        "export class CanvasClient {\n"
        "  constructor(token: string, domain: string, options?: {\n"
        "    maxRetries?: number; retryDelay?: number\n"
        "  }) {\n"
        "    this.token = token;\n"
        "    this.baseURL = `https://${domain}/api/v1`;\n"
        "    this.maxRetries = options?.maxRetries ?? 3;\n"
        "    this.retryDelay = options?.retryDelay ?? 1000;\n\n"
        "    this.client = axios.create({\n"
        "      baseURL: this.baseURL,\n"
        "      headers: {\n"
        "        'Authorization': `Bearer ${token}`,\n"
        "        'Content-Type': 'application/json'\n"
        "      },\n"
        "      timeout: 30000 // 30 second timeout\n"
        "    });\n\n"
        "    this.setupInterceptors();\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    config = tmp_path / "canvas.yaml"
    config.write_text(
        '  env:\n    CANVAS_DOMAIN: "localhost:8080"\n',
        encoding="utf-8",
    )

    assert patch_client(client) is True
    assert patch_config(config) is True
    patched = client.read_text(encoding="utf-8")
    assert "import { PgCanvasRouter }" in patched
    assert "new PgCanvasRouter() as unknown as AxiosInstance" in patched
    assert "const origin = /^https?:\\/\\//i.test(domain)" in patched
    assert "http://localhost:8080" in config.read_text()
    assert patch_client(client) is False
    assert patch_config(config) is False


def test_canvas_patch_preserves_compiled_javascript_indentation(tmp_path: Path) -> None:
    client = tmp_path / "client.js"
    client.write_text(
        "import axios from 'axios';\n"
        "export class CanvasClient {\n"
        "    constructor(domain) {\n"
        "        this.maxRetries = 3;\n"
        "        this.retryDelay = 1000;\n"
        "        this.token = token;\n"
        "        this.baseURL = `https://${domain}/api/v1`;\n"
        "        this.maxRetries = options?.maxRetries ?? 3;\n"
        "        this.retryDelay = options?.retryDelay ?? 1000;\n"
        "        this.client = axios.create({\n"
        "            baseURL: this.baseURL,\n"
        "            headers: {\n"
        "                'Authorization': `Bearer ${token}`,\n"
        "                'Content-Type': 'application/json'\n"
        "            },\n"
        "            timeout: 30000 // 30 second timeout\n"
        "        });\n"
        "        this.setupInterceptors();\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )

    assert patch_client(client) is True
    patched = client.read_text(encoding="utf-8")
    assert "import { PgCanvasRouter }" in patched
    assert "            this.client = new PgCanvasRouter();" in patched
    assert "            const origin = /^https?:\\/\\//i.test(domain)" in patched


def test_canvas_patch_fails_closed_on_upstream_drift(tmp_path: Path) -> None:
    client = tmp_path / "client.ts"
    client.write_text("this.baseURL = domain;\n", encoding="utf-8")

    with pytest.raises(ValueError, match="expected Canvas compatibility source"):
        patch_client(client)
