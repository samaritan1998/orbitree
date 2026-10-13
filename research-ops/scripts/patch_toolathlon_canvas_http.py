#!/usr/bin/env python3
"""Wire Toolathlon's bundled Canvas MCP to its per-session PostgreSQL mock."""

from __future__ import annotations

import argparse
from pathlib import Path

TS_IMPORT_BEFORE = "import axios, { AxiosInstance, AxiosError } from 'axios';"
TS_IMPORT_AFTER = """import axios, { AxiosInstance, AxiosError } from 'axios';
import { PgCanvasRouter } from './pg-canvas-router.js';"""
TS_CLIENT_BEFORE = """    this.token = token;
    this.baseURL = `https://${domain}/api/v1`;
    this.maxRetries = options?.maxRetries ?? 3;
    this.retryDelay = options?.retryDelay ?? 1000;

    this.client = axios.create({
      baseURL: this.baseURL,
      headers: {
        'Authorization': `Bearer ${token}`,
        'Content-Type': 'application/json'
      },
      timeout: 30000 // 30 second timeout
    });

    this.setupInterceptors();"""
TS_CLIENT_AFTER = """    this.token = token;
    this.maxRetries = options?.maxRetries ?? 3;
    this.retryDelay = options?.retryDelay ?? 1000;

    const database = process.env.PG_DATABASE || process.env.PGDATABASE;
    if (database) {
      this.baseURL = `postgresql://${database}`;
      this.client = new PgCanvasRouter() as unknown as AxiosInstance;
    } else {
      const origin = /^https?:\\/\\//i.test(domain) ? domain : `https://${domain}`;
      this.baseURL = `${origin.replace(/\\/+$/, '')}/api/v1`;
      this.client = axios.create({
        baseURL: this.baseURL,
        headers: {
          'Authorization': `Bearer ${token}`,
          'Content-Type': 'application/json'
        },
        timeout: 30000 // 30 second timeout
      });
      this.setupInterceptors();
    }"""
JS_IMPORT_BEFORE = "import axios from 'axios';"
JS_IMPORT_AFTER = """import axios from 'axios';
import { PgCanvasRouter } from './pg-canvas-router.js';"""
JS_CLIENT_BEFORE = """        this.token = token;
        this.baseURL = `https://${domain}/api/v1`;
        this.maxRetries = options?.maxRetries ?? 3;
        this.retryDelay = options?.retryDelay ?? 1000;
        this.client = axios.create({
            baseURL: this.baseURL,
            headers: {
                'Authorization': `Bearer ${token}`,
                'Content-Type': 'application/json'
            },
            timeout: 30000 // 30 second timeout
        });
        this.setupInterceptors();"""
JS_CLIENT_AFTER = """        this.token = token;
        this.maxRetries = options?.maxRetries ?? 3;
        this.retryDelay = options?.retryDelay ?? 1000;
        const database = process.env.PG_DATABASE || process.env.PGDATABASE;
        if (database) {
            this.baseURL = `postgresql://${database}`;
            this.client = new PgCanvasRouter();
        }
        else {
            const origin = /^https?:\\/\\//i.test(domain) ? domain : `https://${domain}`;
            this.baseURL = `${origin.replace(/\\/+$/, '')}/api/v1`;
            this.client = axios.create({
                baseURL: this.baseURL,
                headers: {
                    'Authorization': `Bearer ${token}`,
                    'Content-Type': 'application/json'
                },
                timeout: 30000 // 30 second timeout
            });
            this.setupInterceptors();
        }"""
CONFIG_BEFORE = '    CANVAS_DOMAIN: "localhost:8080"'
CONFIG_AFTER = '    CANVAS_DOMAIN: "http://localhost:8080"'


def _patch_exact(path: Path, before: str, after: str) -> bool:
    source = path.read_text(encoding="utf-8")
    if after in source:
        return False
    if before not in source:
        raise ValueError(f"expected Canvas compatibility source not found: {path}")
    if source.count(before) != 1:
        raise ValueError(f"Canvas compatibility source is ambiguous: {path}")
    path.write_text(source.replace(before, after), encoding="utf-8")
    return True


def patch_client(path: Path) -> bool:
    if path.suffix == ".ts":
        patches = (
            (TS_IMPORT_BEFORE, TS_IMPORT_AFTER),
            (TS_CLIENT_BEFORE, TS_CLIENT_AFTER),
        )
    elif path.suffix == ".js":
        patches = (
            (JS_IMPORT_BEFORE, JS_IMPORT_AFTER),
            (JS_CLIENT_BEFORE, JS_CLIENT_AFTER),
        )
    else:
        raise ValueError(f"unsupported Canvas client source: {path}")
    changed = False
    for before, after in patches:
        changed = _patch_exact(path, before, after) or changed
    return changed


def patch_config(path: Path) -> bool:
    return _patch_exact(path, CONFIG_BEFORE, CONFIG_AFTER)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", type=Path, action="append", default=[])
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    if not args.client and args.config is None:
        parser.error("at least one of --client or --config is required")
    for client in args.client:
        patch_client(client)
    if args.config is not None:
        patch_config(args.config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
