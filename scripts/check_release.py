#!/usr/bin/env python3
"""Heuristic public-release guard, not a guarantee that all secrets are detected.

With a repository, inspect Git index blobs, not potentially different working
files. Before git init, inspect only explicitly publishable files/directories.
Never print matched values. Findings contain a relative path, line, and category.

Optional JSON allowlist: {"version": 1, "entries": [{"path": "exact/path",
"category": "private-network-address", "sha256": "hash-of-entire-line",
"reason": "why this specific example is safe"}]}. Only documented non-secret
categories below may be exempted. For a small synthetic PNG, use category
"synthetic-image" and the hash of the complete file, under examples/synthetic/
or tests/fixtures/synthetic/. Entries cannot exempt credentials or private keys.
The allowlist itself must be staged when scanning a Git repository.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


PUBLIC_DIRS = {"scripts", "tests", "docs", "templates", "schemas", "examples", ".github"}
PUBLIC_FILES = {
    ".gitignore", ".gitattributes", ".env.example", "config.example.json",
    "pyproject.toml", "setup.cfg", "Makefile", "release-allowlist.json", "AGENTS.md",
}
ROOT_DOCUMENT = re.compile(r"(?:README(?:\.[\w-]+)?\.md|LICENSE(?:\.\w+)?|CHANGELOG\.md|CONTRIBUTING\.md|SECURITY\.md|requirements(?:-[\w-]+)?\.txt)")
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_SYNTHETIC_PNG_BYTES = 128 * 1024
MAX_LINE_CHARACTERS = 8192
FORBIDDEN_EXTENSIONS = {
    ".mp4", ".mov", ".mkv", ".webm", ".avi", ".mp3", ".wav", ".flac",
    ".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".onnx", ".gguf",
    ".zip", ".tar", ".gz", ".7z", ".db", ".sqlite", ".sqlite3", ".pem", ".key",
}
RUNTIME_COMPONENTS = {"__pycache__", ".ssh", ".aws", "node_modules", ".venv", ".cache", "browser-profile"}
EXEMPTIBLE = {"private-network-address", "local-identity-path", "synthetic-image"}
PLACEHOLDER = re.compile(r"^(?:|replace[-_]me|example(?:[-_].*)?|your[-_].*|changeme|placeholder|demo(?:[-_].*)?|<[^>]+>|\$\{[^}]+\}|\*+|localhost|127\.0\.0\.1|::1)$", re.I)
IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
IPV6 = re.compile(r"(?<![\w:])(?:[0-9a-fA-F]{0,4}:){2,}[0-9a-fA-F:.]*(?![\w:])")
LOCAL_PATH = re.compile(r"(?:/(?:Users|home)/[^\s/'\"<>]+|[A-Za-z]:[\\/](?:Users|Documents and Settings)[\\/][^\s\\/'\"<>]+|/(?:mnt|data)/[^\s'\"<>]*(?:users|home)/[^\s/'\"<>]+)")
PRIVATE_KEY = re.compile(r"-----BEGIN " + r"(?:[A-Z0-9 ]+ )?PRIVATE KEY-----")
KNOWN_TOKEN = re.compile(r"(?:\bgh[pousr]_" + r"[A-Za-z0-9]{20,}|\bgithub_pat_" + r"[A-Za-z0-9_]{20,}|\bsk-" + r"[A-Za-z0-9_-]{20,}|\bAKIA" + r"[A-Z0-9]{16}\b|\bxox[baprs]-" + r"[A-Za-z0-9-]{15,}|\bhf_" + r"[A-Za-z0-9]{20,}|\bLTAI" + r"[A-Za-z0-9]{12,})")
CREDENTIAL_ASSIGNMENT = re.compile(r"(?i)[\"']?(?:api[_-]?key|access[_-]?token|auth[_-]?token|refresh[_-]?token|client[_-]?secret|secret[_-]?key|password)[\"']?\s*[:=]\s*[\"']([^\"'\n]+)[\"']")
ENV_CREDENTIAL = re.compile(r"^\s*(?:export\s+)?(?:[A-Z][A-Z0-9_]*_)?(?:API_KEY|ACCESS_TOKEN|AUTH_TOKEN|REFRESH_TOKEN|CLIENT_SECRET|SECRET_KEY|PASSWORD)\s*=\s*([^\s#]+)")
BEARER = re.compile(r"(?i)\bBearer\s+([A-Za-z0-9._~-]{16,})")
SSH_ROUTE = re.compile(r"(?<![\w/.-])[A-Za-z][\w.-]*@(?:[A-Za-z0-9][\w.-]*)(?=[:/\s'\"]|$)")
REMOTE_ASSIGNMENT = re.compile(r"(?i)[\"']?(?:remote_host|ssh_host|ssh_user|hostname|identityfile)[\"']?\s*[:=]\s*[\"']([^\"'\n]+)[\"']")
HOST_ASSIGNMENT = re.compile(r"(?i)[\"']host[\"']\s*:\s*[\"']([^\"'\n]+)[\"']")
URL_CREDENTIALS = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s/:]+:[^\s/@]+@", re.I)
UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
DATED_JOB = re.compile(r"(?i)[\"']?(?:job_id|project_id|timeline_id|prompt_id)[\"']?\s*[:=]\s*[\"']([^\"'\n]+)[\"']")


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    category: str


@dataclass(frozen=True)
class Entry:
    path: str
    mode: str
    data: bytes


def publishable(path: str) -> bool:
    p = PurePosixPath(path)
    if p.is_absolute() or ".." in p.parts or not p.parts:
        return False
    if len(p.parts) == 1:
        return path in PUBLIC_FILES or ROOT_DOCUMENT.fullmatch(path) is not None
    return p.parts[0] in PUBLIC_DIRS


def git(root: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.DEVNULL)


def collect_entries(root: Path, source: str = "auto") -> tuple[list[Entry], str]:
    try:
        own_repo = Path(git(root, "rev-parse", "--show-toplevel").decode().strip()).resolve() == root.resolve()
    except (subprocess.CalledProcessError, FileNotFoundError):
        own_repo = False
    if source == "index" and not own_repo:
        raise ValueError("index-required")
    if source != "directory" and own_repo:
        result = []
        for record in git(root, "ls-files", "--stage", "-z").split(b"\0"):
            if not record:
                continue
            header, raw_path = record.split(b"\t", 1)
            mode, oid, stage = header.decode().split()
            path = raw_path.decode("utf-8", "surrogateescape")
            if stage != "0":
                result.append(Entry(path, "unmerged", b""))
            elif mode == "160000":
                result.append(Entry(path, mode, b""))
            else:
                size = int(git(root, "cat-file", "-s", oid))
                data = b"\0" * (MAX_FILE_BYTES + 1) if size > MAX_FILE_BYTES else git(root, "cat-file", "blob", oid)
                result.append(Entry(path, mode, data))
        return result, "index"
    result = []
    for candidate in sorted(root.iterdir()):
        if candidate.name in PUBLIC_DIRS and candidate.is_dir() and not candidate.is_symlink():
            files = candidate.rglob("*")
        else:
            files = [candidate]
        for path in files:
            relative = path.relative_to(root).as_posix()
            if not publishable(relative):
                continue
            if any(part in RUNTIME_COMPONENTS for part in path.relative_to(root).parts) or path.suffix in {".pyc", ".pyo"}:
                continue
            if path.is_symlink():
                result.append(Entry(relative, "120000", str(path.readlink()).encode()))
            elif path.is_file():
                with path.open("rb") as handle:
                    result.append(Entry(relative, "100644", handle.read(MAX_FILE_BYTES + 1)))
    return result, "directory"


def parse_allowlist(data: bytes) -> set[tuple[str, str, str]]:
    obj = json.loads(data)
    if not isinstance(obj, dict) or obj.get("version") != 1 or not isinstance(obj.get("entries"), list):
        raise ValueError("invalid-allowlist")
    entries = set()
    for entry in obj["entries"]:
        if not isinstance(entry, dict):
            raise ValueError("invalid-allowlist")
        path, category, digest, reason = (entry.get(k) for k in ("path", "category", "sha256", "reason"))
        if not all(isinstance(v, str) for v in (path, category, digest, reason)):
            raise ValueError("invalid-allowlist")
        if not publishable(path) or any(c in path for c in "*?[") or category not in EXEMPTIBLE or not re.fullmatch(r"[a-f0-9]{64}", digest) or len(reason.strip()) < 10:
            raise ValueError("invalid-allowlist")
        entries.add((path, category, digest))
    return entries


def text_categories(line: str) -> set[str]:
    result = set()
    if LOCAL_PATH.search(line):
        result.add("local-identity-path")
    if PRIVATE_KEY.search(line):
        result.add("private-key")
    if KNOWN_TOKEN.search(line) or URL_CREDENTIALS.search(line):
        result.add("credential")
    for pattern in (CREDENTIAL_ASSIGNMENT, BEARER):
        if any(not PLACEHOLDER.fullmatch(m.group(1)) for m in pattern.finditer(line)):
            result.add("credential")
    for match in ENV_CREDENTIAL.finditer(line):
        if not PLACEHOLDER.fullmatch(match.group(1).strip("\"'")):
            result.add("credential")
    for match in IPV4.finditer(line):
        try:
            address = ipaddress.ip_address(match.group())
        except ValueError:
            continue
        # Exact loopback is a supported local ComfyUI default. Documentation
        # network ranges are reserved examples; all other private IPs fail.
        if str(address) == "127.0.0.1" or any(address in network for network in (
            ipaddress.ip_network("192.0.2.0/24"), ipaddress.ip_network("198.51.100.0/24"), ipaddress.ip_network("203.0.113.0/24"),
        )):
            continue
        if address.is_private or address.is_link_local or address.is_loopback:
            result.add("private-network-address")
    for match in IPV6.finditer(line):
        try:
            address = ipaddress.ip_address(match.group())
        except ValueError:
            continue
        if str(address) == "::1" or address in ipaddress.ip_network("2001:db8::/32"):
            continue
        if address.is_private or address.is_link_local or address.is_loopback:
            result.add("private-network-address")
    for pattern in (REMOTE_ASSIGNMENT, HOST_ASSIGNMENT):
        if any(not PLACEHOLDER.fullmatch(m.group(1)) for m in pattern.finditer(line)):
            result.add("ssh-connection")
    for match in SSH_ROUTE.finditer(line):
        value = match.group()
        host = value.split("@", 1)[1]
        if host not in {"example.com", "example.org", "example.net", "github.com"} and not host.endswith((".example", ".invalid")):
            result.add("ssh-connection")
    for match in UUID.finditer(line):
        if set(match.group().replace("-", "")) != {"0"}:
            result.add("production-identifier")
    for match in DATED_JOB.finditer(line):
        if re.search(r"20\d{6}", match.group(1)) and not PLACEHOLDER.fullmatch(match.group(1)):
            result.add("production-identifier")
    return result


def safe_symlink(entry: Entry, by_path: dict[str, Entry], visited: set[str] | None = None) -> bool:
    visited = set() if visited is None else visited
    if entry.path in visited:
        return False
    visited.add(entry.path)
    try:
        target = PurePosixPath(entry.data.decode("utf-8"))
    except UnicodeDecodeError:
        return False
    if target.is_absolute() or not str(target) or "\\" in str(target):
        return False
    parts = list(PurePosixPath(entry.path).parent.parts)
    for part in target.parts:
        if part == "..":
            if not parts:
                return False
            parts.pop()
        elif part != ".":
            parts.append(part)
    path = "/".join(parts)
    linked = by_path.get(path)
    if linked is None or not publishable(path):
        return False
    if linked.mode == "120000":
        return safe_symlink(linked, by_path, visited)
    return linked.mode in {"100644", "100755"}


def scan(entries: list[Entry], allowlist: set[tuple[str, str, str]] | None = None) -> list[Finding]:
    allowlist = allowlist or set()
    findings = set()
    by_path = {entry.path: entry for entry in entries}
    for entry in entries:
        path = PurePosixPath(entry.path)
        def flag(category: str, line: int = 0, data: bytes = b"") -> None:
            if (entry.path, category, hashlib.sha256(data).hexdigest()) not in allowlist:
                findings.add(Finding(entry.path, line, category))
        if not publishable(entry.path):
            flag("non-public-path")
        if any(part in RUNTIME_COMPONENTS for part in path.parts) or path.name in {"config.json", ".env", "id_rsa", "id_ed25519"} or path.name.startswith(("cookies", "credentials", "session")):
            flag("runtime-or-auth-file")
        if entry.mode not in {"100644", "100755", "120000"}:
            flag("unsupported-git-entry")
            continue
        if len(entry.data) > MAX_FILE_BYTES:
            flag("oversize-file")
            continue
        if path.suffix.lower() in FORBIDDEN_EXTENSIONS:
            flag("media-model-or-archive")
        if entry.mode == "120000":
            if not safe_symlink(entry, by_path):
                flag("unsafe-symlink")
            continue
        if path.suffix.lower() == ".png":
            allowed_location = entry.path.startswith(("examples/synthetic/", "tests/fixtures/synthetic/"))
            if not allowed_location or len(entry.data) > MAX_SYNTHETIC_PNG_BYTES or not entry.data.startswith(b"\x89PNG\r\n\x1a\n"):
                flag("binary-asset")
            else:
                flag("synthetic-image", data=entry.data)
            continue
        try:
            content = entry.data.decode("utf-8")
        except UnicodeDecodeError:
            flag("binary-asset")
            continue
        if "\0" in content:
            flag("binary-asset")
            continue
        for number, line in enumerate(content.splitlines(), 1):
            if len(line) > MAX_LINE_CHARACTERS:
                flag("oversize-line", number)
                continue
            for category in text_categories(line):
                flag(category, number, line.encode())
    return sorted(findings, key=lambda finding: (finding.path, finding.line, finding.category))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--source", choices=("auto", "index", "directory"), default="auto")
    parser.add_argument("--allowlist", default="release-allowlist.json", help="repository-relative, exact allowlist filename")
    args = parser.parse_args(argv)
    try:
        entries, source = collect_entries(args.root.resolve(), args.source)
        if not entries:
            print(".:0:empty-release-input", file=sys.stderr)
            return 1
        allow_entry = next((entry for entry in entries if entry.path == args.allowlist), None)
        if (args.root / args.allowlist).exists() and allow_entry is None:
            raise ValueError("allowlist-not-in-release-input")
        allowlist = parse_allowlist(allow_entry.data) if allow_entry else set()
        findings = scan(entries, allowlist)
    except (OSError, ValueError, subprocess.CalledProcessError):
        print(".:0:release-input-error", file=sys.stderr)
        return 1
    for finding in findings:
        # JSON escaping prevents control characters in filenames from hiding
        # findings or injecting arbitrary terminal output.
        safe_path = json.dumps(finding.path, ensure_ascii=True)
        print(f"{safe_path}:{finding.line}:{finding.category}")
    if findings:
        return 1
    print(f"Release guard passed ({source}, {len(entries)} files). Heuristic checks are not a complete secret audit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
