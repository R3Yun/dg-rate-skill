#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read XLS completely into a persistent parse workspace."""
import argparse
import json
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import xlrd  # noqa: F401
    HAS_XLRD = True
except ImportError:
    HAS_XLRD = False

from workbook_extract import extract_xls, extract_xlsx


def inspect_xls(path, max_rows=50, sheet_name=None, root=None, chat_id="", message_id=""):
    if not HAS_XLRD:
        return {"error": "xlrd not installed", "hint": "pip install xlrd"}
    try:
        with open(path, "rb") as source:
            signature = source.read(4)
        extractor = extract_xlsx if signature.startswith(b"PK") else extract_xls
        result = extractor(
            path,
            root=root,
            chat_id=chat_id,
            message_id=message_id,
            sheet_name=sheet_name,
            sample_rows=max_rows,
        )
        if extractor is extract_xlsx:
            result.setdefault("source_summary", {})["parser"] = "read-xls"
            result["reading_hint"] = (
                "文件扩展名为 .xls，但内容是 XLSX/ZIP；已按 XLSX 完整提取。 "
                + result.get("reading_hint", "")
            )
        return result
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
    result = inspect_xls(
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