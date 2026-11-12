#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read a bounded page from a persistent rate parse workspace."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from parse_workspace import ParseWorkspace
from workbook_extract import rows_to_markdown


def _load_rows(path: Path, start_row: int, limit: int):
    rows = []
    with path.open("r", encoding="utf-8") as source:
        for line in source:
            if not line.strip():
                continue
            row = json.loads(line)
            row_number = int(row.get("row_number", 0) or 0)
            if row_number < start_row:
                continue
            if len(rows) >= limit:
                break
            rows.append(row)
    return rows


def read_page(root, parse_id, sheet, start_row=1, limit=50, output_format="both", max_chars=50000, all_rows=False, all_sheets=False):
    if start_row < 1:
        raise ValueError("start_row must be >= 1")
    if not all_rows and (limit < 1 or limit > 100):
        raise ValueError("limit must be between 1 and 100")
    workspace = ParseWorkspace(root)
    loaded = workspace.load(parse_id)
    workbook_path = Path(loaded["path"]) / "raw" / "workbook.json"
    workbook = json.loads(workbook_path.read_text(encoding="utf-8"))

    if all_sheets:
        return _read_all_sheets(loaded, workbook, output_format, max_chars, start_row, limit, all_rows)

    selected = None
    for item in workbook.get("sheets", []):
        if sheet in (item.get("sheet_id"), item.get("name")):
            selected = item
            break
    if selected is None:
        raise ValueError(f"sheet not found: {sheet}")
    effective_limit = 10**8 if all_rows else limit
    raw_path = Path(loaded["path"]) / selected["raw_file"]
    rows = _load_rows(raw_path, start_row, effective_limit)
    markdown = rows_to_markdown(selected["name"], rows)
    while rows and len(markdown) > max_chars and len(rows) > 1:
        rows = rows[: max(1, len(rows) // 2)]
        markdown = rows_to_markdown(selected["name"], rows)
    end_row = rows[-1]["row_number"] if rows else start_row - 1
    total_rows = int(selected.get("total_rows", 0) or 0)
    has_more = end_row < total_rows and not all_rows
    result = {
        "code": "PAGE_OK",
        "parse_id": parse_id,
        "revision": loaded["state"]["revision"],
        "sheet": {"sheet_id": selected["sheet_id"], "name": selected["name"]},
        "range": {"start": start_row, "end": end_row, "returned": len(rows), "total": total_rows},
        "has_more": has_more,
        "next_row": end_row + 1 if has_more else None,
    }
    if all_rows:
        result["all_rows_mode"] = True
    if output_format in ("json", "both"):
        result["rows"] = rows
    if output_format in ("markdown", "both"):
        result["content_markdown"] = markdown
    return result


def _read_all_sheets(loaded, workbook, output_format, max_chars, start_row, limit, all_rows):
    """§4.5 方案一 parse_page 批次化: 一次返回所有 sheet (替代 N 次调用)."""
    pages = []
    total_rows_all = 0
    for item in workbook.get("sheets", []):
        raw_path = Path(loaded["path"]) / item["raw_file"]
        effective_limit = 10**8 if all_rows else limit
        rows = _load_rows(raw_path, start_row, effective_limit)
        markdown = rows_to_markdown(item["name"], rows)
        while rows and len(markdown) > max_chars and len(rows) > 1:
            rows = rows[: max(1, len(rows) // 2)]
            markdown = rows_to_markdown(item["name"], rows)
        end_row = rows[-1]["row_number"] if rows else start_row - 1
        total_rows = int(item.get("total_rows", 0) or 0)
        page = {
            "sheet_id": item["sheet_id"],
            "sheet_name": item.get("name", item["sheet_id"]),
            "range": {"start": start_row, "end": end_row, "returned": len(rows), "total": total_rows},
            "has_more": end_row < total_rows and not all_rows,
        }
        if output_format in ("json", "both"):
            page["rows"] = rows
        if output_format in ("markdown", "both"):
            page["content_markdown"] = markdown
        pages.append(page)
        total_rows_all += total_rows
    return {
        "code": "ALL_PAGES_OK",
        "parse_id": loaded["state"].get("parse_id", ""),
        "revision": loaded["state"]["revision"],
        "all_sheets_mode": True,
        "page_count": len(pages),
        "total_rows": total_rows_all,
        "pages": pages,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--parse-id", required=True)
    parser.add_argument("--sheet", required=True)
    parser.add_argument("--start-row", type=int, default=1)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--format", choices=["json", "markdown", "md", "both"], default="both")
    parser.add_argument("--max-chars", type=int, default=50000)
    parser.add_argument("--all-rows", action="store_true",
                        help="§4.5 方案一: 一次返回该 sheet 所有行 (替代分页)")
    parser.add_argument("--all-sheets", action="store_true",
                        help="§4.5 方案一: 一次返回所有 sheet (替代多次调用)")
    parser.add_argument("--workspace-root", default=None)
    args = parser.parse_args()
    try:
        result = read_page(
            args.workspace_root,
            args.parse_id,
            args.sheet,
            start_row=args.start_row,
            limit=args.limit,
            output_format=args.format,
            max_chars=max(1000, args.max_chars),
            all_rows=args.all_rows,
            all_sheets=args.all_sheets,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"code": "PAGE_ERROR", "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())