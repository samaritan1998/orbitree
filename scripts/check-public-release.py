#!/usr/bin/env python3
"""Check Git-tracked release files without reading ignored runtime data.

This is a metadata/data-file guard, not a replacement for a secret scanner.
Extra literal deny terms can be supplied in a private newline-delimited file.
Reports contain locations and categories, never matched credential values.
"""

from __future__ import annotations

import argparse
import ipaddress
import re
import subprocess
from pathlib import Path


IP = re.compile(rb"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
INTERNAL_DOMAIN = re.compile(rb"[\w.-]+(?:-int|\.internal|\.corp)\.[a-z]{2,}", re.I)
MACHINE_STORAGE = re.compile(rb"/mnt/cfs[\w-]*/|/workspace/[a-z]+-[0-9]+/", re.I)
RUNTIME_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".log", ".jsonl", ".pem", ".key", ".p12"}


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", *args])


def problems(name: str, content: bytes, extra: list[bytes]) -> list[str]:
    path = Path(name)
    issues = []
    if path.suffix in RUNTIME_SUFFIXES or path.name in {".env", "auth.json", ".npmrc"}:
        issues.append("runtime-or-credential-file")
    if name.startswith(("wide/", "research-ops/data/", "research-ops/deploy/ltp/")):
        issues.append("private-runtime-directory")
    if b"\x00" in content:
        if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".ico"}:
            issues.append("unreviewed-binary-file")
        return issues
    if INTERNAL_DOMAIN.search(content):
        issues.append("internal-domain")
    if MACHINE_STORAGE.search(content):
        issues.append("machine-specific-storage")
    for match in IP.finditer(content):
        try:
            address = ipaddress.ip_address(match.group().decode())
        except ValueError:
            continue
        # Documentation networks, wildcard binds, and loopback are allowed.
        private = any(address in network for network in (
            ipaddress.IPv4Network((10 << 24, 8)),
            ipaddress.IPv4Network(((172 << 24) | (16 << 16), 12)),
            ipaddress.IPv4Network(((192 << 24) | (168 << 16), 16)),
        ))
        if private:
            issues.append("private-network-address")
            break
    if any(term in content or term in name.encode() for term in extra):
        issues.append("private-deny-term")
    return issues


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all-history", action="store_true")
    parser.add_argument("--forbidden-file", type=Path)
    args = parser.parse_args()
    root = Path(git("rev-parse", "--show-toplevel").decode().strip())
    terms = []
    if args.forbidden_file:
        terms = [line.strip() for line in args.forbidden_file.read_bytes().splitlines() if line.strip()]
    checked = 0
    failures = []
    if args.all_history:
        # Scan every reachable blob at every historical path, including refs
        # other than HEAD. Deleted files must not escape this check.
        seen = set()
        for commit in git("rev-list", "--all").decode().splitlines():
            for record in git("ls-tree", "-r", "-z", commit).split(b"\0"):
                if not record:
                    continue
                meta, path = record.split(b"\t", 1)
                mode, kind, oid = meta.split()
                name = path.decode()
                key = (oid, name)
                if kind != b"blob" or key in seen:
                    continue
                seen.add(key)
                checked += 1
                issues = problems(name, git("cat-file", "blob", oid.decode()), terms)
                if issues:
                    failures.append((name, issues))
            identity = git("show", "-s", "--format=%an%n%ae%n%cn%n%ce%n%B", commit)
            if any(term in identity for term in terms):
                failures.append(("commit metadata", ["private-deny-term"]))
    else:
        for record in git("ls-files", "-z").split(b"\0"):
            if not record:
                continue
            name = record.decode()
            checked += 1
            issues = problems(name, (root / name).read_bytes(), terms)
            if issues:
                failures.append((name, issues))
    for name, issues in failures:
        print(f"FAIL {name}: {', '.join(issues)}")
    print(f"Checked {checked} tracked blobs; {len(failures)} failed.")
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
