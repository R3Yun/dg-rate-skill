#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Persistent business-task state for resumable rate imports."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

TASK_SCHEMA_VERSION = "rate-task/v1"
INDEX_SCHEMA_VERSION = "rate-task-index/v1"
ACTIVE_STATUSES = {"进行中", "待确认"}
TERMINAL_STATUS = "已结束"
TASK_ID_RE = re.compile(r"^rate_[A-Za-z0-9_-]+$")


def _now() -> str:
    if ZoneInfo is not None:
        return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _atomic_json(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.tmp-{uuid.uuid4().hex}"
    encoded = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        with temporary.open("wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _deep_merge(target: Dict[str, Any], updates: Dict[str, Any]) -> None:
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_merge(target[key], value)
        else:
            target[key] = value


def _validate_terminal_task(task: Dict[str, Any]) -> None:
    end_reason = str(task.get("end_reason") or "").strip()
    if end_reason == "abandoned":
        return
    if end_reason != "completed":
        raise TaskStateError("terminal task requires end_reason=completed or abandoned")
    execution = task.get("execution") or {}
    try:
        written_count = int(execution.get("written_count") or 0)
        verified_count = int(execution.get("verified_count") or 0)
    except (TypeError, ValueError) as exc:
        raise TaskStateError("terminal task write counts must be integers") from exc
    if written_count < 1:
        raise TaskStateError("completed task requires written_count >= 1")
    if verified_count < written_count:
        raise TaskStateError("completed task requires verified_count >= written_count")
    if execution.get("last_action") != "write_verified":
        raise TaskStateError("completed task requires execution.last_action=write_verified")
    source_path = str((task.get("source") or {}).get("source_path") or "").lower()
    if source_path.endswith((".xls", ".xlsx")) and not (task.get("identifiers") or {}).get("parse_id"):
        raise TaskStateError("completed workbook task requires identifiers.parse_id")


class TaskStateError(RuntimeError):
    pass


class ActiveTaskExists(TaskStateError):
    def __init__(self, task: Dict[str, Any]):
        super().__init__(f"active task exists: {task.get('task_id')}")
        self.task = task


class TaskNotFound(TaskStateError):
    pass


def is_user_stopped(task: Dict[str, Any]) -> bool:
    """P0-A (D16): detect business STOP/暂停 directive.

    Returns True when either:
      - task['pending_action'] == 'user_stopped'
      - task['execution']['last_action'] == 'awaiting_user_confirmation'
    """
    if not isinstance(task, dict):
        return False
    if str(task.get('pending_action') or '') == 'user_stopped':
        return True
    execution = task.get('execution') or {}
    if str(execution.get('last_action') or '') == 'awaiting_user_confirmation':
        return True
    return False


class RateTaskStore:
    """Small persistent task registry; chat history is never the source of truth."""

    def __init__(self, root: Optional[str] = None):
        configured = root or os.environ.get("DG_RATE_TASK_ROOT") or "/home/node/.openclaw/workspace/runtime/rate-tasks"
        self.root = Path(configured).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "index.json"
        if not self.index_path.exists():
            _atomic_json(self.index_path, {"schema_version": INDEX_SCHEMA_VERSION, "active_by_chat": {}})

    def _load_index(self) -> Dict[str, Any]:
        index = _read_json(self.index_path)
        if index.get("schema_version") != INDEX_SCHEMA_VERSION:
            raise TaskStateError("unsupported task index schema")
        index.setdefault("active_by_chat", {})
        return index

    def _save_index(self, index: Dict[str, Any]) -> None:
        _atomic_json(self.index_path, index)

    def _task_dir(self, task_id: str) -> Path:
        if not TASK_ID_RE.fullmatch(str(task_id or "")):
            raise TaskStateError(f"invalid task_id: {task_id!r}")
        return self.root / task_id

    def create(self, *, chat_id: str, source_file: str, source_sha256: str = "", parse_id: str = "", source_path: str = "") -> Dict[str, Any]:
        chat_id = str(chat_id or "").strip()
        source_file = str(source_file or "").strip()
        source_path = str(source_path or "").strip()
        if not chat_id or chat_id.lower() in {"undefined", "null", "none"}:
            raise TaskStateError("chat_id is required")
        if not source_file:
            raise TaskStateError("source_file is required")
        index = self._load_index()
        active = index["active_by_chat"].get(chat_id)
        if active:
            raise ActiveTaskExists(active)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        digest = (source_sha256 or hashlib.sha256(source_file.encode("utf-8")).hexdigest())[:8]
        task_id = f"rate_{stamp}_{digest}_{uuid.uuid4().hex[:6]}"
        task_dir = self._task_dir(task_id)
        task_dir.mkdir()
        for name in ("source.jsonl", "draft.jsonl"):
            (task_dir / name).touch()
        now = _now()
        task = {
            "schema_version": TASK_SCHEMA_VERSION,
            "task_id": task_id,
            "chat_id": chat_id,
            "status": "进行中",
            "pending_action": None,
            "source": {"file_name": source_file, "source_sha256": source_sha256, "source_path": source_path},
            "identifiers": {"parse_id": parse_id, "draft_id": ""},
            "confirmed_fields": {},
            "pending_questions": [],
            "execution": {"last_action": "task_created", "last_error": None, "written_count": 0, "verified_count": 0},
            "created_at": now,
            "updated_at": now,
        }
        _atomic_json(task_dir / "task.json", task)
        index["active_by_chat"][chat_id] = {"task_id": task_id, "status": task["status"], "updated_at": now}
        self._save_index(index)
        return task

    def open(self, *, chat_id: str, source_file: str, source_sha256: str = "", parse_id: str = "", source_path: str = "") -> Dict[str, Any]:
        """Atomically decide whether a new source may enter this chat."""
        active = self.find_active(chat_id)
        if active:
            raise ActiveTaskExists(active)
        return self.create(
            chat_id=chat_id, source_file=source_file, source_sha256=source_sha256,
            parse_id=parse_id, source_path=source_path,
        )

    def load(self, task_id: str) -> Dict[str, Any]:
        path = self._task_dir(task_id) / "task.json"
        if not path.is_file():
            raise TaskNotFound(task_id)
        return _read_json(path)

    def find_active(self, chat_id: str) -> Optional[Dict[str, Any]]:
        entry = self._load_index().get("active_by_chat", {}).get(str(chat_id or ""))
        if not entry:
            return None
        try:
            task = self.load(entry["task_id"])
        except TaskNotFound:
            return None
        if task.get("status") not in ACTIVE_STATUSES:
            return None
        return task

    def update(self, task_id: str, *, status: Optional[str] = None, pending_action: Optional[str] = None, updates: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        task = self.load(task_id)
        if status is not None and status not in ACTIVE_STATUSES | {TERMINAL_STATUS}:
            raise TaskStateError(f"unsupported task status: {status}")
        incoming = updates or {}
        for key in incoming:
            if key in {"task_id", "chat_id", "schema_version", "created_at", "updated_at", "status"}:
                raise TaskStateError(f"reserved task field: {key}")
        _deep_merge(task, incoming)
        if status is not None:
            task["status"] = status
        if pending_action is not None:
            task["pending_action"] = pending_action
        if task.get("status") == TERMINAL_STATUS:
            _validate_terminal_task(task)
        task["updated_at"] = _now()
        _atomic_json(self._task_dir(task_id) / "task.json", task)
        index = self._load_index()
        chat_id = task["chat_id"]
        if task["status"] == TERMINAL_STATUS:
            index["active_by_chat"].pop(chat_id, None)
        else:
            index["active_by_chat"][chat_id] = {"task_id": task_id, "status": task["status"], "updated_at": task["updated_at"]}
        self._save_index(index)
        return task


def _main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Persistent rate import task state")
    parser.add_argument("--root", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    open_task = sub.add_parser("open")
    open_task.add_argument("--chat-id", required=True)
    open_task.add_argument("--source-file", required=True)
    open_task.add_argument("--source-sha256", default="")
    open_task.add_argument("--parse-id", default="")
    open_task.add_argument("--source-path", default="")

    create = sub.add_parser("create")
    create.add_argument("--chat-id", required=True)
    create.add_argument("--source-file", required=True)
    create.add_argument("--source-sha256", default="")
    create.add_argument("--parse-id", default="")
    create.add_argument("--source-path", default="")

    show = sub.add_parser("show")
    show.add_argument("task_id")

    find = sub.add_parser("find")
    find.add_argument("--chat-id", required=True)

    update = sub.add_parser("update")
    update.add_argument("task_id")
    update.add_argument("--status", choices=sorted(ACTIVE_STATUSES | {TERMINAL_STATUS}))
    update.add_argument("--pending-action")
    update.add_argument("--updates", default="{}")

    close = sub.add_parser("close")
    close.add_argument("task_id")
    close.add_argument("--reason", choices=["completed", "abandoned"], default="abandoned")
    close.add_argument("--note", default="")

    args = parser.parse_args()
    store = RateTaskStore(args.root)
    try:
        if args.command == "open":
            result = store.open(
                chat_id=args.chat_id, source_file=args.source_file,
                source_sha256=args.source_sha256, parse_id=args.parse_id,
                source_path=args.source_path,
            )
        elif args.command == "create":
            result = store.create(
                chat_id=args.chat_id, source_file=args.source_file,
                source_sha256=args.source_sha256, parse_id=args.parse_id,
                source_path=args.source_path,
            )
        elif args.command == "show":
            result = store.load(args.task_id)
        elif args.command == "find":
            result = store.find_active(args.chat_id)
        elif args.command == "close":
            existing = store.load(args.task_id)
            if existing.get("status") == TERMINAL_STATUS:
                result = existing
            else:
                note = args.note
                if note:
                    incoming = {"end_reason": args.reason, "execution": {"last_action": args.reason, "note": note}}
                else:
                    incoming = {"end_reason": args.reason, "execution": {"last_action": args.reason}}
                result = store.update(args.task_id, status=TERMINAL_STATUS, updates=incoming)
        else:
            result = store.update(args.task_id, status=args.status, pending_action=args.pending_action, updates=json.loads(args.updates))
    except ActiveTaskExists as exc:
        print(json.dumps({"code": "ACTIVE_TASK_EXISTS", "task": exc.task}, ensure_ascii=False, indent=2))
        raise SystemExit(2)
    except TaskStateError as exc:
        print(json.dumps({"code": "TASK_STATE_ERROR", "message": str(exc)}, ensure_ascii=False, indent=2))
        raise SystemExit(2)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _main()
