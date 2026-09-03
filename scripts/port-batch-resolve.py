#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""port-batch-resolve — D6 工具类原子工具 (D6-7)

批量港口解析.

CLI:
  port-batch-resolve --json ''{"queries":["CNSHA","THBKK","BDCGP"]}''
  echo ''{"queries":["CNSHA","THBKK"]}'' | port-batch-resolve --stdin

Exit codes:
  0 — OK (即使有些 query 找不到)
  2 — 参数/JSON 错误
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from port_resolve import resolve  # 用下划线文件名


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=None)
    ap.add_argument("--stdin", action="store_true")
    ap.add_argument("--queries", nargs="*", default=None)
    args = ap.parse_args()

    queries = []
    if args.json:
        try:
            payload = json.loads(args.json)
            queries = payload.get("queries") or payload.get("items") or []
        except Exception as e:
            print(json.dumps({"error": f"invalid --json: {e}"}, ensure_ascii=False))
            sys.exit(2)
    elif args.stdin:
        data = sys.stdin.read().strip()
        try:
            payload = json.loads(data)
            queries = payload.get("queries") or payload.get("items") or []
        except Exception as e:
            print(json.dumps({"error": f"invalid stdin JSON: {e}"}, ensure_ascii=False))
            sys.exit(2)
    elif args.queries:
        queries = args.queries
    else:
        print(json.dumps({"error": "no queries"}, ensure_ascii=False))
        sys.exit(2)

    if not queries:
        print(json.dumps({"error": "empty queries"}, ensure_ascii=False))
        sys.exit(2)

    results = [resolve(q) for q in queries]
    hit = sum(1 for r in results if not r.get("not_found") and not r.get("error"))
    miss = len(results) - hit

    out = {"results": results, "total": len(results), "hit": hit, "miss": miss}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    sys.exit(0)


if __name__ == "__main__":
    main()