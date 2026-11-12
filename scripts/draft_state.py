#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Update, select, and resume persistent normalized rate drafts."""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from draft_builder import FIELD_NAMES, _atomic_write_jsonl
from parse_workspace import ParseWorkspace, StaleWorkspaceError, _now_iso
from rate_io import NormalizedRateEntry, classify_entry


PROTECTED_FIELDS = {"draft_record_id", "_provenance", "_classification", "_draft_schema"}


class DraftStateError(ValueError):
    code = "DRAFT_STATE_ERROR"


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.is_file():
        raise DraftStateError(f"draft file not found: {path}")
    rows = []
    with path.open("r", encoding="utf-8") as source:
        for line_number, line in enumerate(source, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise DraftStateError(f"draft JSONL line {line_number} is not object")
            rows.append(value)
    return rows


def _normalize_field_updates(fields: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    if not isinstance(fields, dict) or not fields:
        raise DraftStateError("fields must be a non-empty object")
    normalized = {}
    for field, specification in fields.items():
        if field not in FIELD_NAMES or field in PROTECTED_FIELDS:
            raise DraftStateError(f"unsupported draft field: {field}")
        if isinstance(specification, dict) and "value" in specification:
            normalized[field] = {
                "value": specification.get("value"),
                "source": str(specification.get("source") or "business_reply"),
            }
        else:
            normalized[field] = {"value": specification, "source": "business_reply"}
    return normalized


def _matches(entry: Dict[str, Any], filters: Optional[Dict[str, Any]]) -> bool:
    if not filters:
        return True
    if not isinstance(filters, dict):
        raise DraftStateError("filter must be an object")
    draft_ids = filters.get("draft_record_ids")
    if draft_ids is not None:
        if not isinstance(draft_ids, list):
            raise DraftStateError("draft_record_ids must be a list")
        if entry.get("draft_record_id") not in set(str(item) for item in draft_ids):
            return False
    source_ids = filters.get("source_row_ids")
    if source_ids is not None:
        if not isinstance(source_ids, list):
            raise DraftStateError("source_row_ids must be a list")
        source_row_id = (entry.get("_provenance") or {}).get("source_row_id")
        if source_row_id not in set(str(item) for item in source_ids):
            return False
    for field, expected in filters.items():
        if field in ("draft_record_ids", "source_row_ids"):
            continue
        if field not in FIELD_NAMES:
            raise DraftStateError(f"unsupported filter field: {field}")
        actual = entry.get(field)
        if isinstance(expected, list):
            if actual not in expected:
                return False
        elif str(actual or "").strip().casefold() != str(expected or "").strip().casefold():
            return False
    return True


def _reclassify(entries: List[Dict[str, Any]], parse_id: str) -> Dict[str, Any]:
    p0_details = []
    p1_details = []
    p2_details = []
    missing_counter = Counter()
    for entry in entries:
        classification = classify_entry(NormalizedRateEntry.from_dict(entry))
        entry["_classification"] = classification
        base = {
            "draft_record_id": entry.get("draft_record_id"),
            "source_row_id": (entry.get("_provenance") or {}).get("source_row_id"),
        }
        if classification["p0_missing"]:
            p0_details.append({**base, "missing": classification["p0_missing"]})
        if classification["p1_missing"]:
            p1_details.append({**base, "missing": classification["p1_missing"]})
        if classification["p2_missing"]:
            p2_details.append({**base, "missing": classification["p2_missing"]})
        for value in classification["p0_missing"] + classification["p1_missing"] + classification["p2_missing"]:
            missing_counter[value] += 1
    return {
        "schema_version": "rate-draft-summary/v1",
        "parse_id": parse_id,
        "valid_entries": len(entries),
        "p0_missing_records": len(p0_details),
        "p1_missing_records": len(p1_details),
        "p2_warning_records": len(p2_details),
        "missing_field_counts": dict(sorted(missing_counter.items())),
        "p0_details": p0_details,
        "p1_details": p1_details,
        "p2_details": p2_details,
    }


def update_draft(
    root: Optional[str],
    parse_id: str,
    fields: Dict[str, Any],
    *,
    expected_revision: int,
    filters: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    workspace = ParseWorkspace(root)
    loaded = workspace.load(parse_id)
    current_revision = int(loaded["state"].get("revision", 0))
    if int(expected_revision) != current_revision:
        raise StaleWorkspaceError(int(expected_revision), current_revision)
    updates = _normalize_field_updates(fields)
    base = Path(loaded["path"])
    entries_path = base / "draft" / "entries.jsonl"
    entries = _read_jsonl(entries_path)
    matched = [entry for entry in entries if _matches(entry, filters)]
    if not matched:
        raise DraftStateError("filter matched 0 draft records")
    for entry in matched:
        provenance = entry.setdefault("_provenance", {})
        constants = provenance.setdefault("constants", {})
        for field, specification in updates.items():
            entry[field] = specification["value"]
            constants[field] = {
                "value": specification["value"],
                "source": specification["source"],
            }
    summary = _reclassify(entries, parse_id)
    _atomic_write_jsonl(entries_path, entries)
    workspace.write_json(parse_id, "draft/missing-fields.json", summary)
    status = "awaiting_user_fields" if summary["p0_missing_records"] else "draft_ready"
    state = workspace.update_state(
        parse_id,
        expected_revision=expected_revision,
        status=status,
        phase="draft",
        last_action="draft_updated",
        next_action="collect_missing_fields" if summary["p0_missing_records"] else "review_draft",
        updates={
            "draft_summary": {
                "valid_entries": summary["valid_entries"],
                "p0_missing_records": summary["p0_missing_records"],
                "p1_missing_records": summary["p1_missing_records"],
                "p2_warning_records": summary["p2_warning_records"],
            }
        },
    )
    return {
        "code": "DRAFT_UPDATED",
        "parse_id": parse_id,
        "revision": state["revision"],
        "state": state["status"],
        "next_action": state["next_action"],
        "matched": len(matched),
        "updated_fields": sorted(updates),
        "p0_missing_records": summary["p0_missing_records"],
        "p1_missing_records": summary["p1_missing_records"],
        "p2_warning_records": summary["p2_warning_records"],
        "missing_field_counts": summary["missing_field_counts"],
    }


def select_draft(
    root: Optional[str],
    parse_id: str,
    *,
    expected_revision: int,
    filters: Dict[str, Any],
    expected_count: Optional[int] = None,
) -> Dict[str, Any]:
    workspace = ParseWorkspace(root)
    loaded = workspace.load(parse_id)
    current_revision = int(loaded["state"].get("revision", 0))
    if int(expected_revision) != current_revision:
        raise StaleWorkspaceError(int(expected_revision), current_revision)
    entries = _read_jsonl(Path(loaded["path"]) / "draft" / "entries.jsonl")
    matched = [entry for entry in entries if _matches(entry, filters)]
    if not matched:
        raise DraftStateError("filter matched 0 draft records")
    if expected_count is not None and len(matched) != int(expected_count):
        raise DraftStateError(
            f"selection count mismatch: expected {expected_count}, matched {len(matched)}"
        )
    selection = {
        "schema_version": "rate-draft-selection/v1",
        "selection_id": f"selection_{uuid.uuid4().hex[:16]}",
        "parse_id": parse_id,
        "revision": current_revision + 1,
        "filter": filters,
        "matched": len(matched),
        "draft_record_ids": [entry["draft_record_id"] for entry in matched],
        "created_at": _now_iso(),
    }
    workspace.write_json(parse_id, "write/selection.json", selection)
    state = workspace.update_state(
        parse_id,
        expected_revision=expected_revision,
        phase="selection",
        last_action="selection_created",
        next_action="review_selection",
        updates={
            "selection": {
                "selection_id": selection["selection_id"],
                "matched": selection["matched"],
            }
        },
    )
    selection["revision"] = state["revision"]
    return {"code": "SELECTION_CREATED", **selection}


def show_draft(
    root: Optional[str],
    parse_id: str,
) -> Dict[str, Any]:
    """D69-fix (2026-08-11): 返回 parse workspace 全量 draft entries (预览/写库的权威数据源).

    供 batch_preview 无 payload 时自动读全量 draft, 确保预览展示的就是最终要写的那批完整数据
    (不依赖 LLM 手动传 payload — 可可曾只传部分 payload 导致预览不全)。
    """
    workspace = ParseWorkspace(root)
    loaded = workspace.load(parse_id)
    entries = _read_jsonl(Path(loaded["path"]) / "draft" / "entries.jsonl")
    return {
        "code": "DRAFT_SHOW",
        "parse_id": parse_id,
        "revision": int(loaded["state"].get("revision", 0)),
        "total": len(entries),
        "entries": entries,
    }


def resume_parse(
    root: Optional[str],
    *,
    parse_id: Optional[str] = None,
    chat_id: Optional[str] = None,
) -> Dict[str, Any]:
    workspace = ParseWorkspace(root)
    if parse_id:
        loaded = workspace.load(parse_id)
    elif chat_id is not None:
        loaded = workspace.find_resumable(chat_id)
        if loaded is None:
            return {"code": "NO_RESUMABLE_PARSE", "chat_id": chat_id}
    else:
        raise DraftStateError("parse_id or chat_id is required")
    manifest = loaded["manifest"]
    state = loaded["state"]
    base = Path(loaded["path"])
    missing = {}
    if (base / "draft" / "missing-fields.json").is_file():
        missing = json.loads((base / "draft" / "missing-fields.json").read_text(encoding="utf-8"))
    selection = None
    if (base / "write" / "selection.json").is_file():
        selection = json.loads((base / "write" / "selection.json").read_text(encoding="utf-8"))
    return {
        "code": "RESUME_AVAILABLE",
        "parse_id": manifest["parse_id"],
        "source_file": manifest["source_file"],
        "source_sha256": manifest["source_sha256"],
        "chat_id": manifest.get("chat_id", ""),
        "message_id": manifest.get("message_id", ""),
        "state": state.get("status"),
        "phase": state.get("phase"),
        "revision": state.get("revision"),
        "next_action": state.get("next_action"),
        "draft_summary": state.get("draft_summary") or {},
        "missing_field_counts": missing.get("missing_field_counts") or {},
        "p0_details": missing.get("p0_details") or [],
        "selection": selection,
    }


def _json_arg(value: str) -> Dict[str, Any]:
    raw = value
    if raw.startswith("@"):
        raw = Path(raw[1:]).read_text(encoding="utf-8")
    loaded = json.loads(raw)
    if not isinstance(loaded, dict):
        raise DraftStateError("argument must be JSON object")
    return loaded


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace-root", default=None)
    commands = parser.add_subparsers(dest="command", required=True)

    update = commands.add_parser("update")
    update.add_argument("--parse-id", required=True)
    update.add_argument("--expected-revision", required=True, type=int)
    update.add_argument("--fields", required=True)
    update.add_argument("--filter", default="{}")

    select = commands.add_parser("select")
    select.add_argument("--parse-id", required=True)
    select.add_argument("--expected-revision", required=True, type=int)
    select.add_argument("--filter", required=True)
    select.add_argument("--expected-count", type=int, default=None)

    resume = commands.add_parser("resume")
    resume.add_argument("--parse-id", default=None)
    resume.add_argument("--chat-id", default=None)

    show = commands.add_parser("show")
    show.add_argument("--parse-id", required=True)

    args = parser.parse_args()
    try:
        if args.command == "update":
            result = update_draft(
                args.workspace_root,
                args.parse_id,
                _json_arg(args.fields),
                expected_revision=args.expected_revision,
                filters=_json_arg(args.filter),
            )
        elif args.command == "select":
            result = select_draft(
                args.workspace_root,
                args.parse_id,
                expected_revision=args.expected_revision,
                filters=_json_arg(args.filter),
                expected_count=args.expected_count,
            )
        elif args.command == "show":
            result = show_draft(args.workspace_root, args.parse_id)
        else:
            result = resume_parse(
                args.workspace_root, parse_id=args.parse_id, chat_id=args.chat_id
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except StaleWorkspaceError as exc:
        print(json.dumps({
            "code": exc.code,
            "expected_revision": exc.expected_revision,
            "current_revision": exc.current_revision,
            "error": str(exc),
        }, ensure_ascii=False, indent=2))
        return 3
    except Exception as exc:
        print(json.dumps({
            "code": getattr(exc, "code", "DRAFT_STATE_ERROR"),
            "error": str(exc),
        }, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    sys.exit(main())