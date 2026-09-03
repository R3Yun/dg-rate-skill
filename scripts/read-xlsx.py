#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read XLSX completely into a persistent parse workspace."""
import argparse
import json
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import openpyxl  # noqa: F401
    HAS_OPENPYXL = True
except ImportError:
    HAS_OPENPYXL = False

from workbook_extract import extract_xlsx


def inspect_xlsx(path, max_rows=50, sheet_name=None, root=None, chat_id="", message_id=""):
    if not HAS_OPENPYXL:
        return {"error": "openpyxl not installed", "hint": "pip install openpyxl"}
    try:
        return extract_xlsx(
            path,
            root=root,
            chat_id=chat_id,
            message_id=message_id,
            sheet_name=sheet_name,
            sample_rows=max_rows,
        )
    except Exception as exc:
        return {"error": f"load failed: {exc}", "trace": traceback.format_exc()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("file")
    parser.add_argument("--max-rows", type=int, default=10, help="summary sample rows per sheet")
    parser.add_argument("--sheet", default=None)
    parser.add_argument("--workspace-root", default=None)
    parser.add_argument("--chat-id", default="")
    parser.add_argument("--message-id", default="")
    args = parser.parse_args()
    if not os.path.exists(args.file):
        print(json.dumps({"error": f"file not found: {args.file}"}))
        return 2
    result = inspect_xlsx(
        args.file,
        max_rows=max(0, args.max_rows),
        sheet_name=args.sheet,
        root=args.workspace_root,
        chat_id=args.chat_id,
        message_id=args.message_id,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())