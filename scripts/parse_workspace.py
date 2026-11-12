#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Persistent workspace primitives for resumable rate-file parsing."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None


WORKSPACE_SCHEMA_VERSION = "rate-parse-workspace/v1"
STATE_SCHEMA_VERSION = "rate-parse-state/v1"
DEFAULT_ROOT = "/home/node/.openclaw/workspace/runtime/rate-parses"
TERMINAL_STATUSES = {"cancelled", "completed"}
ALLOWED_STATUSES = {
    "created",
    "extracted",
    "mapping_required",
    "draft_ready",
    "awaiting_user_fields",
    "ready",
    "failed_recoverable",
    "cancelled",
    "writing",
    "verifying",
    "completed",
}
_PARSE_ID_RE = re.compile(r"^parse_[A-Za-z0-9_-]+$")


class ParseWorkspaceError(RuntimeError):
    code = "WORKSPACE_ERROR"


class WorkspaceNotFoundError(ParseWorkspaceError):
    code = "WORKSPACE_NOT_FOUND"


class WorkspaceCorruptError(ParseWorkspaceError):
    code = "WORKSPACE_CORRUPT"


class StaleWorkspaceError(ParseWorkspaceError):
    code = "STALE_TASK"

    def __init__(self, expected_revision: int, current_revision: int):
        super().__init__(
            f"expected revision {expected_revision}, current revision {current_revision}"
        )
        self.expected_revision = expected_revision
        self.current_revision = current_revision


class UnsafeParseIdError(ParseWorkspaceError):
    code = "UNSAFE_PARSE_ID"


def _now_iso() -> str:
    if ZoneInfo is not None:
        return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.tmp-{uuid.uuid4().hex}"
    try:
        with temporary.open("wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        try:
            directory_fd = os.open(str(path.parent), os.O_RDONLY)
        except OSError:
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()


def _atomic_write_json(path: Path, value: Dict[str, Any]) -> None:
    encoded = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    _atomic_write_bytes(path, encoded + b"\n")


def _read_json(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        raise WorkspaceNotFoundError(f"missing workspace file: {path}")
    try:
        with path.open("r", encoding="utf-8") as source:
            value = json.load(source)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkspaceCorruptError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise WorkspaceCorruptError(f"workspace file is not an object: {path}")
    return value


def _validate_parse_id(parse_id: str) -> str:
    value = str(parse_id or "").strip()
    if not _PARSE_ID_RE.fullmatch(value):
        raise UnsafeParseIdError(f"invalid parse_id: {parse_id!r}")
    return value


class ParseWorkspace:
    """Create, load, update, and locate persistent parse workspaces."""

    subdirectories = ("source", "raw", "preview", "mapping", "draft", "write")

    def __init__(self, root: Optional[str] = None):
        configured_root = root or os.environ.get("DG_RATE_PARSE_ROOT") or DEFAULT_ROOT
        self.root = Path(configured_root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _workspace_dir(self, parse_id: str) -> Path:
        safe_id = _validate_parse_id(parse_id)
        candidate = (self.root / safe_id).resolve()
        if candidate.parent != self.root:
            raise UnsafeParseIdError(f"parse_id escapes workspace root: {parse_id!r}")
        return candidate

    def create(
        self,
        source_path: str,
        *,
        chat_id: str = "",
        message_id: str = "",
        copy_source: bool = True,
        parse_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        source = Path(source_path).expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(str(source))

        source_hash = _sha256_file(source)
        created_at = _now_iso()
        if parse_id is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            parse_id = f"parse_{timestamp}_{source_hash[:8]}_{uuid.uuid4().hex[:6]}"
        workspace_dir = self._workspace_dir(parse_id)
        if workspace_dir.exists():
            raise FileExistsError(str(workspace_dir))

        workspace_dir.mkdir(parents=False)
        try:
            for name in self.subdirectories:
                (workspace_dir / name).mkdir()

            stored_source = source
            if copy_source:
                stored_source = workspace_dir / "source" / source.name
                shutil.copy2(source, stored_source)

            manifest = {
                "schema_version": WORKSPACE_SCHEMA_VERSION,
                "parse_id": parse_id,
                "source_file": source.name,
                "source_path": str(stored_source),
                "original_source_path": str(source),
                "source_sha256": source_hash,
                "source_size": source.stat().st_size,
                "file_type": source.suffix.lower().lstrip("."),
                "chat_id": str(chat_id or ""),
                "message_id": str(message_id or ""),
                "created_at": created_at,
            }
            state = {
                "schema_version": STATE_SCHEMA_VERSION,
                "parse_id": parse_id,
                "status": "created",
                "phase": "workspace",
                "revision": 1,
                "last_action": "workspace_created",
                "next_action": "extract_workbook",
                "created_at": created_at,
                "updated_at": created_at,
                "error": None,
            }
            _atomic_write_json(workspace_dir / "manifest.json", manifest)
            _atomic_write_json(workspace_dir / "state.json", state)
            return {"manifest": manifest, "state": state, "path": str(workspace_dir)}
        except Exception:
            shutil.rmtree(workspace_dir, ignore_errors=True)
            raise

    def load(self, parse_id: str) -> Dict[str, Any]:
        workspace_dir = self._workspace_dir(parse_id)
        if not workspace_dir.is_dir():
            raise WorkspaceNotFoundError(f"workspace not found: {parse_id}")
        manifest = _read_json(workspace_dir / "manifest.json")
        state = _read_json(workspace_dir / "state.json")
        if manifest.get("parse_id") != parse_id or state.get("parse_id") != parse_id:
            raise WorkspaceCorruptError(f"parse_id mismatch in workspace: {parse_id}")
        return {"manifest": manifest, "state": state, "path": str(workspace_dir)}

    def update_state(
        self,
        parse_id: str,
        *,
        expected_revision: int,
        status: Optional[str] = None,
        phase: Optional[str] = None,
        last_action: Optional[str] = None,
        next_action: Optional[str] = None,
        error: Any = None,
        updates: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        workspace_dir = self._workspace_dir(parse_id)
        state_path = workspace_dir / "state.json"
        state = _read_json(state_path)
        current_revision = int(state.get("revision", 0))
        if int(expected_revision) != 0 and int(expected_revision) != current_revision:
            raise StaleWorkspaceError(int(expected_revision), current_revision)
        if status is not None and status not in ALLOWED_STATUSES:
            raise ValueError(f"unsupported workspace status: {status}")

        reserved = {"schema_version", "parse_id", "revision", "created_at"}
        for key, value in (updates or {}).items():
            if key in reserved:
                raise ValueError(f"state field cannot be overwritten: {key}")
            state[key] = value
        if status is not None:
            state["status"] = status
        if phase is not None:
            state["phase"] = phase
        if last_action is not None:
            state["last_action"] = last_action
        if next_action is not None:
            state["next_action"] = next_action
        if error is not None:
            state["error"] = error
        elif status != "failed_recoverable":
            state["error"] = None
        state["revision"] = current_revision + 1
        state["updated_at"] = _now_iso()
        _atomic_write_json(state_path, state)
        return state

    def write_json(self, parse_id: str, relative_path: str, value: Dict[str, Any]) -> str:
        target = self._safe_artifact_path(parse_id, relative_path)
        _atomic_write_json(target, value)
        return str(target)

    def write_text(self, parse_id: str, relative_path: str, value: str) -> str:
        target = self._safe_artifact_path(parse_id, relative_path)
        _atomic_write_bytes(target, str(value).encode("utf-8"))
        return str(target)

    def append_jsonl(self, parse_id: str, relative_path: str, rows: Iterable[Dict[str, Any]]) -> str:
        target = self._safe_artifact_path(parse_id, relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as output:
            for row in rows:
                output.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            output.flush()
            os.fsync(output.fileno())
        return str(target)

    def _safe_artifact_path(self, parse_id: str, relative_path: str) -> Path:
        workspace_dir = self._workspace_dir(parse_id)
        if not workspace_dir.is_dir():
            raise WorkspaceNotFoundError(f"workspace not found: {parse_id}")
        relative = Path(str(relative_path or ""))
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise ValueError(f"unsafe workspace artifact path: {relative_path!r}")
        target = (workspace_dir / relative).resolve()
        if workspace_dir not in target.parents:
            raise ValueError(f"workspace artifact escapes root: {relative_path!r}")
        return target

    def find_resumable(self, chat_id: str) -> Optional[Dict[str, Any]]:
        wanted_chat = str(chat_id or "")
        candidates = []
        for child in self.root.iterdir():
            if not child.is_dir() or not child.name.startswith("parse_"):
                continue
            try:
                loaded = self.load(child.name)
            except ParseWorkspaceError:
                continue
            manifest = loaded["manifest"]
            state = loaded["state"]
            if manifest.get("chat_id", "") != wanted_chat:
                continue
            if state.get("status") in TERMINAL_STATUSES:
                continue
            candidates.append(loaded)
        if not candidates:
            return None
        candidates.sort(
            key=lambda item: str(item["state"].get("updated_at") or ""),
            reverse=True,
        )
        return candidates[0]


def _main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Persistent rate parse workspace")
    parser.add_argument("--root", default=None)
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_parser = subparsers.add_parser("create")
    create_parser.add_argument("source")
    create_parser.add_argument("--chat-id", default="")
    create_parser.add_argument("--message-id", default="")

    show_parser = subparsers.add_parser("show")
    show_parser.add_argument("parse_id")

    resume_parser = subparsers.add_parser("resume")
    resume_parser.add_argument("--chat-id", required=True)

    args = parser.parse_args()
    workspace = ParseWorkspace(args.root)
    if args.command == "create":
        result = workspace.create(args.source, chat_id=args.chat_id, message_id=args.message_id)
    elif args.command == "show":
        result = workspace.load(args.parse_id)
    else:
        result = workspace.find_resumable(args.chat_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _main()