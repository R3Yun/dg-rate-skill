#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
preflight.py - 关键字段预检 (CLI)

用法:
  # 单条
  dg-rate-query preflight --json entry.json

  # 多条 (jq 链)
  dg-rate-query preflight --json entries.json --list

  # 从 stdin
  cat entry.json | dg-rate-query preflight --stdin
"""
import sys
import os
import argparse
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rate_io import (
    NormalizedRateEntry,
    CRITICAL_FIELDS,
    OPTIONAL_FIELDS,
    get_missing_critical_fields,
    get_missing_optional_fields,
    is_critical_complete,
    preflight_summary,
)


def check_entry(entry_dict):
    """v3.8: 校验单条 entry, 返回 critical/p1/p2/transition 四段
    Returns:
        (ok, summary_dict, entry)
        ok: bool - P0 阻塞字段齐全 (可写入)
        summary_dict: preflight_summary 结果 (含 p1_missing / p2_missing)
        entry: NormalizedRateEntry 对象
    """
    entry = NormalizedRateEntry(**entry_dict)
    summary = preflight_summary(entry)
    # 状态迁移检查 (如果有 status 字段)
    # v3.7+: TRANSITION_REQUIRED 已删除 (审核人字段已移除), 无状态迁移必填校验
    return summary["ok"], summary, entry


def main():
    ap = argparse.ArgumentParser(description="关键字段预检")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--json", help="entry JSON 路径")
    src.add_argument("--stdin", action="store_true", help="从 stdin 读 JSON")
    ap.add_argument("--list", action="store_true",
                    help="支持多条 (entries list)")
    args = ap.parse_args()

    if args.json:
        with open(args.json, encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = json.load(sys.stdin)

    is_entries_payload = isinstance(data, dict) and isinstance(data.get("entries"), list)
    is_list_payload = isinstance(data, list)
    if args.list or is_entries_payload or is_list_payload:
        # 多条
        items = data if isinstance(data, list) else data.get("entries", [])
        results = []
        all_ok = True
        for i, item in enumerate(items):
            ok, summary, _ = check_entry(item)
            if not ok:
                all_ok = False
            results.append({
                "index": i,
                "ok": ok,
                # v3.8 P0/P1/P2 三段
                "critical_missing": summary["critical_missing"],
                "p1_missing": summary.get("p1_missing", []),
                "p2_missing": summary.get("p2_missing", []),
                "status": summary.get("status", "unknown"),
                "optional_missing": summary["optional_missing"],
                "pol": item.get("pol"),
                "pod": item.get("pod"),
                "carrier": item.get("carrier"),
            })
        out = {
            "ok": all_ok,
            "total": len(items),
            "passed": sum(1 for r in results if r["ok"]),
            "failed": sum(1 for r in results if not r["ok"]),
            # v3.8 分类统计
            "p0_blocked_count": sum(1 for r in results if r["status"] == "p0_blocked"),
            "p1_downgrade_count": sum(1 for r in results if r["status"] == "p1_downgrade"),
            "complete_count": sum(1 for r in results if r["status"] == "complete"),
            # 兼容字段
            "critical_count": len(CRITICAL_FIELDS),
            "optional_count": len(OPTIONAL_FIELDS),
            "details": results,
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0 if all_ok else 1

    # 单条
    ok, summary, entry = check_entry(data)
    out = {
        "ok": ok,
        # v3.8 P0/P1/P2
        "critical_missing": summary["critical_missing"],
        "critical_count": len(summary["critical_missing"]),
        "p1_missing": summary.get("p1_missing", []),
        "p1_count": len(summary.get("p1_missing", [])),
        "p2_missing": summary.get("p2_missing", []),
        "p2_count": len(summary.get("p2_missing", [])),
        "status": summary.get("status", "unknown"),
        # 兼容
        "optional_missing": summary["optional_missing"],
        "optional_count": len(summary["optional_missing"]),
        "warning": summary["warning"],
        "critical_fields": [l for _, l in CRITICAL_FIELDS],
        "optional_fields_count": len(OPTIONAL_FIELDS),
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(2)
