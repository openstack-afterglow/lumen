#!/usr/bin/env python3
"""Check or stamp the source snapshot recorded in ARCHITECTURE.md.

The guard uses only Python's standard library and Git. Working checks hash the
working tree; staged checks hash the index and read the indexed architecture
review. Stamping never stages files or changes the index.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import BinaryIO

EXIT_OK = 0
EXIT_STALE = 1
EXIT_ERROR = 2
ARCHITECTURE = b"ARCHITECTURE.md"
MAX_DOCUMENT_SIZE = 32 * 1024 * 1024
CHUNK_SIZE = 1024 * 1024
REQUIRED_HEADINGS = (
    b"Overview",
    b"Development status",
    b"System context",
    b"Code map",
    b"Runtime flows",
    b"Data and contracts",
    b"Deployment and operations",
    b"Security boundaries",
    b"Development and verification",
    b"Change guide",
    b"Maintenance",
    b"Glossary",
)
START_MARKER = b"<!-- architecture-review:start -->"
END_MARKER = b"<!-- architecture-review:end -->"
MARKER_RE = re.compile(
    rb"<!-- architecture-review:start -->[ \t\r\n]*```json[ \t\r\n]*(.*?)[ \t\r\n]*```[ \t\r\n]*<!-- architecture-review:end -->",
    re.DOTALL,
)
HEADING_RE = re.compile(rb"^## ([^\r\n]+?)[ \t]*\r?$", re.MULTILINE)
TIMESTAMP_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


class GuardError(Exception):
    """Operational failure mapped to exit status 2."""


class StaleDocument(Exception):
    """Missing, malformed, or stale architecture review mapped to status 1."""


def _run_git(root: Path, *args: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise GuardError("Git is unavailable") from exc
    if result.returncode != 0:
        raise GuardError("Git command failed")
    return result.stdout


def _repository_root(script_root: Path) -> Path:
    raw = _run_git(script_root, "rev-parse", "--show-toplevel").rstrip(b"\r\n")
    if not raw:
        raise GuardError("Git root is unavailable")
    try:
        git_root = Path(os.fsdecode(raw)).resolve()
        expected = script_root.resolve()
    except (OSError, ValueError) as exc:
        raise GuardError("Git root is invalid") from exc
    if git_root != expected:
        raise GuardError("Script and Git roots differ")
    return git_root


def _split_nul(data: bytes) -> list[bytes]:
    if not data:
        return []
    if not data.endswith(b"\0"):
        raise GuardError("Git returned a malformed path list")
    return data[:-1].split(b"\0")


def _is_excluded(path: bytes) -> bool:
    if path == ARCHITECTURE:
        return True
    if path.startswith((b"docs/", b"openspec/")):
        return True
    if b"/" not in path and path.lower().endswith(b".md"):
        return path not in {b"AGENTS.md", b"CLAUDE.md"}
    return False


def _index_entries(root: Path) -> dict[bytes, tuple[bytes, bytes]]:
    raw = _run_git(root, "ls-files", "--stage", "-z")
    entries: dict[bytes, tuple[bytes, bytes]] = {}
    for item in _split_nul(raw):
        try:
            header, path = item.split(b"\t", 1)
            mode, oid, stage_raw = header.split(b" ", 2)
            stage = int(stage_raw)
        except (ValueError, UnicodeError) as exc:
            raise GuardError("Git returned a malformed index entry") from exc
        if stage != 0:
            raise GuardError("Index contains an unmerged entry")
        if path in entries:
            raise GuardError("Index contains duplicate entries")
        entries[path] = (mode, oid)
    return entries


def _working_paths(root: Path) -> list[bytes]:
    raw = _run_git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    return sorted({path for path in _split_nul(raw) if path and not _is_excluded(path)})


def _working_mode(path: bytes) -> bytes:
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise GuardError("Cannot stat source") from exc
    if stat.S_ISLNK(info.st_mode):
        return b"120000"
    if stat.S_ISREG(info.st_mode):
        return b"100755" if info.st_mode & stat.S_IXUSR else b"100644"
    if stat.S_ISDIR(info.st_mode):
        raise GuardError("Git submodules and source directories are unsupported")
    raise GuardError("Unsupported source file type")


def _working_content_digest(path: bytes, mode: bytes) -> bytes:
    digest = hashlib.sha256()
    try:
        if mode == b"120000":
            digest.update(os.readlink(path))
        else:
            with open(path, "rb") as source:
                while chunk := source.read(CHUNK_SIZE):
                    digest.update(chunk)
    except OSError as exc:
        raise GuardError("Cannot read source") from exc
    return digest.digest()


def _read_exact(stream: BinaryIO, length: int) -> bytes:
    chunks: list[bytes] = []
    remaining = length
    while remaining:
        chunk = stream.read(min(CHUNK_SIZE, remaining))
        if not chunk:
            raise GuardError("Git object stream ended early")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


class _CatFile:
    def __init__(self, root: Path) -> None:
        try:
            self.process = subprocess.Popen(
                ["git", "cat-file", "--batch"],
                cwd=root,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            raise GuardError("Cannot start Git object stream") from exc

    def _header(self, oid: bytes) -> tuple[BinaryIO, int]:
        stdin = self.process.stdin
        stdout = self.process.stdout
        if stdin is None or stdout is None:
            raise GuardError("Git object stream is unavailable")
        try:
            stdin.write(oid + b"\n")
            stdin.flush()
            header = stdout.readline()
            returned_oid, kind, size_raw = header.rstrip(b"\n").split(b" ", 2)
            size = int(size_raw)
        except (OSError, ValueError) as exc:
            raise GuardError("Git returned a malformed object header") from exc
        if returned_oid != oid or kind != b"blob" or size < 0:
            raise GuardError("Staged source is not a blob")
        return stdout, size

    def hash_blob(self, oid: bytes) -> bytes:
        stdout, size = self._header(oid)
        digest = hashlib.sha256()
        remaining = size
        while remaining:
            try:
                chunk = stdout.read(min(CHUNK_SIZE, remaining))
            except OSError as exc:
                raise GuardError("Cannot read staged object") from exc
            if not chunk:
                raise GuardError("Git object stream ended early")
            digest.update(chunk)
            remaining -= len(chunk)
        if _read_exact(stdout, 1) != b"\n":
            raise GuardError("Git object stream is malformed")
        return digest.digest()

    def read_blob(self, oid: bytes, *, max_size: int) -> bytes:
        stdout, size = self._header(oid)
        if size > max_size:
            raise GuardError("Staged architecture document is too large")
        content = _read_exact(stdout, size)
        if _read_exact(stdout, 1) != b"\n":
            raise GuardError("Git object stream is malformed")
        return content

    def close(self, *, abort: bool = False) -> None:
        process = self.process
        if abort:
            process.terminate()
        elif process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                process.terminate()
        try:
            returncode = process.wait(timeout=2)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.wait()
            if not abort:
                raise GuardError("Git object stream did not exit") from exc
            return
        if returncode and not abort:
            raise GuardError("Git object stream failed")


def _aggregate_digest(records: Iterable[tuple[bytes, bytes, bytes]]) -> tuple[str, int]:
    digest = hashlib.sha256()
    count = 0
    for path, mode, content_digest in records:
        count += 1
        for field in (path, mode, content_digest.hex().encode("ascii")):
            digest.update(len(field).to_bytes(8, "big"))
            digest.update(field)
    return digest.hexdigest(), count


def _working_digest(root: Path) -> tuple[str, int]:
    entries = _index_entries(root)
    for path, (mode, _) in entries.items():
        if mode == b"160000" and not _is_excluded(path):
            raise GuardError("Git submodules are unsupported source boundaries")

    root_bytes = os.fsencode(root)
    records: list[tuple[bytes, bytes, bytes]] = []
    for relative in _working_paths(root):
        absolute = os.path.join(root_bytes, relative)
        if not os.path.lexists(absolute):
            continue
        mode = _working_mode(absolute)
        records.append((relative, mode, _working_content_digest(absolute, mode)))
    return _aggregate_digest(records)


def _staged_digest(root: Path, *, include_document: bool) -> tuple[str, int, bytes | None]:
    entries = _index_entries(root)
    source_entries = sorted((path, entry) for path, entry in entries.items() if not _is_excluded(path))
    reader = _CatFile(root)
    records: list[tuple[bytes, bytes, bytes]] = []
    document: bytes | None = None
    failed = True
    try:
        for path, (mode, oid) in source_entries:
            if mode == b"160000":
                raise GuardError("Git submodules are unsupported source boundaries")
            if mode not in {b"100644", b"100755", b"120000"}:
                raise GuardError("Unsupported staged source mode")
            records.append((path, mode, reader.hash_blob(oid)))
        if include_document:
            entry = entries.get(ARCHITECTURE)
            if entry is None:
                raise StaleDocument("ARCHITECTURE.md is missing from the index")
            mode, oid = entry
            if mode not in {b"100644", b"100755"}:
                raise GuardError("ARCHITECTURE.md has an unsupported mode")
            document = reader.read_blob(oid, max_size=MAX_DOCUMENT_SIZE)
        failed = False
    finally:
        reader.close(abort=failed)
    digest, count = _aggregate_digest(records)
    return digest, count, document


def _read_working_document(root: Path) -> bytes:
    path = root / os.fsdecode(ARCHITECTURE)
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise StaleDocument("ARCHITECTURE.md is not a regular file")
        if info.st_size > MAX_DOCUMENT_SIZE:
            raise GuardError("ARCHITECTURE.md is too large")
        return path.read_bytes()
    except FileNotFoundError as exc:
        raise StaleDocument("ARCHITECTURE.md is missing") from exc
    except StaleDocument:
        raise
    except OSError as exc:
        raise GuardError("Cannot read ARCHITECTURE.md") from exc


def _heading_positions(document: bytes) -> dict[bytes, int]:
    matches = list(HEADING_RE.finditer(document))
    positions: dict[bytes, int] = {}
    ordered: list[int] = []
    for required in REQUIRED_HEADINGS:
        match = next((candidate for candidate in matches if candidate.group(1) == required), None)
        if match is None:
            raise StaleDocument("Required architecture heading is missing")
        positions[required] = match.start()
        ordered.append(match.start())
    if ordered != sorted(ordered):
        raise StaleDocument("Architecture headings are out of order")
    return positions


def _validate_review_payload(payload: object, expected_digest: str | None) -> None:
    if not isinstance(payload, dict):
        raise StaleDocument("Architecture review JSON is not an object")
    required = {"schema_version", "source_sha256", "reviewed_at", "summary"}
    if set(payload) != required or payload.get("schema_version") != 1:
        raise StaleDocument("Architecture review JSON schema is invalid")

    source_digest = payload.get("source_sha256")
    reviewed_at = payload.get("reviewed_at")
    summary = payload.get("summary")
    if not isinstance(source_digest, str) or not HEX64_RE.fullmatch(source_digest):
        raise StaleDocument("Architecture review digest is invalid")
    if expected_digest is not None and source_digest != expected_digest:
        raise StaleDocument("Architecture source snapshot is stale")
    if not isinstance(reviewed_at, str) or not TIMESTAMP_RE.fullmatch(reviewed_at):
        raise StaleDocument("Architecture review timestamp is invalid")
    try:
        parsed = dt.datetime.strptime(reviewed_at, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise StaleDocument("Architecture review timestamp is invalid") from exc
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != reviewed_at:
        raise StaleDocument("Architecture review timestamp is invalid")
    if not isinstance(summary, str) or not summary.strip():
        raise StaleDocument("Architecture review summary is empty")


def _review_span(
    document: bytes,
    *,
    expected_digest: str | None,
    allow_missing: bool,
) -> tuple[int, int] | None:
    headings = _heading_positions(document)
    start_count = document.count(START_MARKER)
    end_count = document.count(END_MARKER)
    if start_count == 0 and end_count == 0:
        if allow_missing:
            return None
        raise StaleDocument("Architecture review block is missing")
    if start_count != 1 or end_count != 1:
        raise StaleDocument("Architecture review markers are duplicated or incomplete")
    match = MARKER_RE.search(document)
    if match is None:
        raise StaleDocument("Architecture review block is malformed")
    if not (headings[b"Maintenance"] < match.start() < match.end() < headings[b"Glossary"]):
        raise StaleDocument("Architecture review block is outside Maintenance")
    try:
        payload = json.loads(match.group(1).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StaleDocument("Architecture review JSON is malformed") from exc
    _validate_review_payload(payload, expected_digest)
    return match.start(), match.end()


def _review_block(source_digest: str, summary: str) -> bytes:
    payload = {
        "schema_version": 1,
        "source_sha256": source_digest,
        "reviewed_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "summary": summary,
    }
    encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    return START_MARKER + b"\n```json\n" + encoded + b"\n```\n" + END_MARKER


def _stamped_document(document: bytes, source_digest: str, summary: str) -> bytes:
    span = _review_span(document, expected_digest=None, allow_missing=True)
    block = _review_block(source_digest, summary)
    if span is not None:
        start, end = span
        return document[:start] + block + document[end:]
    glossary = _heading_positions(document)[b"Glossary"]
    return document[:glossary] + b"\n\n" + block + b"\n\n" + document[glossary:]


def _atomic_write_document(root: Path, content: bytes) -> None:
    path = root / "ARCHITECTURE.md"
    try:
        info = path.lstat()
    except OSError as exc:
        raise GuardError("Cannot stat ARCHITECTURE.md for stamping") from exc
    if not stat.S_ISREG(info.st_mode):
        raise GuardError("ARCHITECTURE.md is not a regular file")

    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=root, prefix=".architecture.", suffix=".tmp", delete=False) as handle:
            temporary = handle.name
            handle.write(content)
            handle.flush()
            os.fchmod(handle.fileno(), stat.S_IMODE(info.st_mode))
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        directory_fd = os.open(root, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        raise GuardError("Cannot atomically write ARCHITECTURE.md") from exc
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check ARCHITECTURE.md source freshness")
    parser.add_argument("--staged", action="store_true", help="read source and review document from the Git index")
    parser.add_argument("--stamp", action="store_true", help="write a fresh review block to the working document")
    parser.add_argument("--summary", help="non-empty review summary used with --stamp")
    args = parser.parse_args(argv)
    if args.summary is not None and not args.stamp:
        parser.error("--summary requires --stamp")
    if args.stamp and (args.summary is None or not args.summary.strip()):
        parser.error("--stamp requires a non-empty --summary")
    return args


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parse_args(sys.argv[1:] if argv is None else argv)
        root = _repository_root(Path(__file__).resolve().parent.parent)
        if args.staged:
            source_digest, count, staged_document = _staged_digest(root, include_document=not args.stamp)
            document = _read_working_document(root) if args.stamp else staged_document
            if document is None:
                raise StaleDocument("ARCHITECTURE.md is missing from the index")
        else:
            source_digest, count = _working_digest(root)
            document = _read_working_document(root)

        mode = "staged" if args.staged else "working"
        if args.stamp:
            _atomic_write_document(root, _stamped_document(document, source_digest, args.summary))
            print(f"stamped {mode} source_sha256={source_digest} files={count}")
            return EXIT_OK

        _review_span(document, expected_digest=source_digest, allow_missing=False)
        print(f"checked {mode} source_sha256={source_digest} files={count}")
        return EXIT_OK
    except StaleDocument as exc:
        print(
            f"architecture check failed: {exc}. Review ARCHITECTURE.md, then run the matching --stamp command.",
            file=sys.stderr,
        )
        return EXIT_STALE
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else EXIT_ERROR
    except GuardError as exc:
        print(f"architecture guard error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except Exception:
        print("architecture guard error: unexpected failure", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
