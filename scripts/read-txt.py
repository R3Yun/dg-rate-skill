#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""read-txt — D6 读类原子工具 (D6-4)

读取 .txt / .md → 输出内容. 自动编码探测.

"同上" 解析留给 LLM (read-* 不做语义解析, 这是 D1 三层架构约束).
"""
import argparse
import json
import os
import sys
import traceback


def _detect_encoding(path):
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030", "latin-1"):
        try:
            with open(path, "r", encoding=enc) as f:
                f.read(8192)
            return enc
        except (UnicodeDecodeError, LookupError):
            continue
    return "utf-8"


def inspect_txt(path, max_bytes=200_000, encoding=None):
    enc = encoding or _detect_encoding(path)
    try:
        with open(path, "r", encoding=enc, errors="replace") as f:
            content = f.read(max_bytes)
    except Exception as e:
        return {"error": f"read failed: {e}"}

    lines = content.splitlines()
    total = len(lines)
    non_empty = sum(1 for ln in lines if ln.strip())
    size_kb = round(os.path.getsize(path) / 1024, 1)
    truncated = total > 200
    return {
        "source_summary": {
            "file": os.path.basename(path),
            "size_kb": size_kb,
            "encoding": enc,
            "line_count": total,
            "non_empty_lines": non_empty,
            "truncated": truncated,
            "parser": "read-txt",
        },
        "content_markdown": content,  # txt 全文本输出 (无表格化)
        "reading_hint": f"txt 编码 {enc}, 总 {total} 行 (非空 {non_empty})"
                          + (", 已截断" if truncated else ""),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--max-bytes", type=int, default=200_000)
    ap.add_argument("--encoding", default=None)
    args = ap.parse_args()

    if not os.path.exists(args.file):
        print(json.dumps({"error": f"file not found: {args.file}"}))
        sys.exit(2)

    result = inspect_txt(args.file, args.max_bytes, args.encoding)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(json.dumps({"error": str(e), "trace": traceback.format_exc()}, ensure_ascii=False))
        sys.exit(1)