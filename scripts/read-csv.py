#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""read-csv — D6 读类原子工具 (D6-3)

读取 .csv / .tsv → 输出 markdown.
自动探测编码 (utf-8-sig / utf-8 / gbk / gb18030) 与分隔符.
"""
import argparse
import csv
import json
import os
import sys
import traceback


def _detect_encoding(path):
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
        try:
            with open(path, "r", encoding=enc) as f:
                f.read(2048)
            return enc
        except (UnicodeDecodeError, LookupError):
            continue
    return "utf-8"


def _detect_delimiter(path, encoding):
    with open(path, "r", encoding=encoding) as f:
        head = f.read(4096)
    candidates = [",", "\t", ";", "|"]
    counts = {d: head.count(d) for d in candidates}
    return max(counts, key=counts.get)


def inspect_csv(path, max_rows=100, encoding=None, delimiter=None):
    enc = encoding or _detect_encoding(path)
    sep = delimiter or _detect_delimiter(path, enc)
    try:
        with open(path, "r", encoding=enc, errors="replace") as f:
            reader = csv.reader(f, delimiter=sep)
            rows = []
            for i, row in enumerate(reader):
                rows.append([c.strip() for c in row])
                if i >= max_rows + 5:
                    break
    except Exception as e:
        return {"error": f"read failed: {e}"}

    if not rows:
        return {
            "source_summary": {"file": os.path.basename(path), "size_kb": 0, "encoding": enc, "delimiter": repr(sep), "row_count": 0, "parser": "read-csv"},
            "content_markdown": "(empty file)",
            "reading_hint": "empty csv"
        }

    header = rows[0]
    md_parts = ["| " + " | ".join(header) + " |"]
    md_parts.append("|" + "|".join(["---"] * len(header)) + "|")
    for row in rows[1:max_rows+1]:
        cells = (row + [""] * len(header))[:len(header)]
        md_parts.append("| " + " | ".join(cells) + " |")

    size_kb = round(os.path.getsize(path) / 1024, 1)
    return {
        "source_summary": {
            "file": os.path.basename(path),
            "size_kb": size_kb,
            "encoding": enc,
            "delimiter": repr(sep),
            "row_count": len(rows) - 1,
            "parser": "read-csv",
        },
        "content_markdown": "\n".join(md_parts),
        "reading_hint": f"csv 编码 {enc}, 分隔符 {repr(sep)}, {len(rows)-1} 行数据"
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--max-rows", type=int, default=100)
    ap.add_argument("--encoding", default=None)
    ap.add_argument("--delimiter", default=None)
    args = ap.parse_args()

    if not os.path.exists(args.file):
        print(json.dumps({"error": f"file not found: {args.file}"}))
        sys.exit(2)

    result = inspect_csv(args.file, args.max_rows, args.encoding, args.delimiter)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(json.dumps({"error": str(e), "trace": traceback.format_exc()}, ensure_ascii=False))
        sys.exit(1)