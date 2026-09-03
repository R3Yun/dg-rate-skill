#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""delete-record - D6 写类原子工具

按筛选条件删除 FCL 表记录。

CLI:
  delete-record --rate-no NO.001,NO.002          # 按运价编号
  delete-record --carrier IAL --pod NHAVA        # 组合筛选
  delete-record --pod BANGKOK --dry-run          # 预演
"""
import argparse
import json
import os
import subprocess
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def run_cmd(cmd, timeout=30):
    """运行命令并返回 stdout"""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except Exception as e:
        return -1, "", str(e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-token", default="Eje8bWtVdaPPPosu0GQcPclQnut")
    ap.add_argument("--table-id", default="tblnCWVGvCfFHW6m")
    ap.add_argument("--rate-no", default="", help="按运价编号筛选 (支持逗号分隔)")
    ap.add_argument("--record-id", default="", help="按记录 ID 精确删除 (支持逗号分隔多个, WS-151 事故清理需要)")
    ap.add_argument("--carrier", default="", help="按船公司模糊匹配")
    ap.add_argument("--pod", default="", help="按目的港模糊匹配")
    ap.add_argument("--pol", default="", help="按起运港模糊匹配")
    ap.add_argument("--import-after", default="", help="按导入时间起始 (YYYY-MM-DD)")
    ap.add_argument("--import-before", default="", help="按导入时间截止 (YYYY-MM-DD)")
    ap.add_argument("--dry-run", action="store_true", help="预演模式: 只列出待删除的记录 ID，不实际删除")
    ap.add_argument("--yes", action="store_true", help="跳过确认直接删除 (默认需要 --yes)")
    args = ap.parse_args()

    if not any([args.rate_no, args.record_id, args.carrier, args.pod, args.pol, args.import_after, args.import_before]):
        print(json.dumps({"code": "NO_FILTER", "error": "必须至少指定一个筛选条件"}, ensure_ascii=False))
        sys.exit(1)

    # P0 保护 (2026-08-12, 误删 109 条教训): 宽泛条件禁止实际删除 (查询前拦截)
    # 只有 import-after/before 时间范围, 无精确标识 (rate-no/record-id) → 拒删
    # 防止 "delete_record --import-after 2026-08-11" 一刀切删全天记录
    # WS-151 事故 (2026-09-01): --record-id 精确删除 — 11 条 recvtWi* 重复记录清理
    # 只能按 record_id 逐个删 (这些记录无 rate_no, 且 carrier+时间窗被守卫拦截).
    has_precise = bool(args.rate_no or args.record_id)
    has_time_filter = bool(args.import_after or args.import_before)
    if has_time_filter and not has_precise and not args.dry_run:
        print(json.dumps({
            "code": "WIDE_FILTER_BLOCKED",
            "ok": False,
            "deleted_count": 0,
            "would_delete_count": 0,
            "message": "宽泛时间范围条件 (import-after/before) 未附带精确标识 (rate-no/record-id), 禁止实际删除. "
                       "请先 --dry-run 核对将删记录, 或补充 rate-no/record-id 精确条件后重试.",
            "filters": {
                "rate_no": args.rate_no,
                "record_id": args.record_id,
                "carrier": args.carrier,
                "pod": args.pod,
                "pol": args.pol,
                "import_after": args.import_after,
                "import_before": args.import_before,
            },
        }, ensure_ascii=False, indent=2))
        sys.exit(3)

    # 查找匹配的记录
    from find_records_filtered import find_records
    # WS-151 事故 (2026-09-01): --record-id 直接指定删除目标 (逗号分隔), 跳过筛选查找.
    # 用于 11 条 recvtWi* 重复记录清理 — 这些记录无 rate_no, 只能按 record_id 精确删.
    if args.record_id:
        record_ids = [rid.strip() for rid in args.record_id.split(",") if rid.strip()]
        err = None
    else:
        record_ids, err = find_records(
            base_token=args.base_token,
            table_id=args.table_id,
            rate_no=args.rate_no,
            carrier=args.carrier,
            pod=args.pod,
            pol=args.pol,
            import_after=args.import_after,
            import_before=args.import_before,
        )
    if err:
        print(json.dumps({"code": "FIND_ERROR", "error": err}, ensure_ascii=False))
        sys.exit(1)

    if not record_ids:
        print(json.dumps({
            "code": "NO_MATCH",
            "ok": False,
            "record_ids": [],
            "deleted_count": 0,
            "message": "未找到符合条件的记录",
            "filters": {
                "rate_no": args.rate_no,
                "record_id": args.record_id,
                "carrier": args.carrier,
                "pod": args.pod,
                "pol": args.pol,
                "import_after": args.import_after,
                "import_before": args.import_before,
            },
        }, ensure_ascii=False, indent=2))
        sys.exit(0)

    # P0 保护: 删除上限 — 超过 20 条强制 dry-run (即使带 --yes)
    if len(record_ids) > 20 and not args.dry_run:
        print(json.dumps({
            "code": "DELETE_LIMIT_EXCEEDED",
            "ok": False,
            "deleted_count": 0,
            "would_delete_count": len(record_ids),
            "limit": 20,
            "message": f"将删除 {len(record_ids)} 条 > 上限 20, 禁止直接删除. 请先 --dry-run 核对, 或收窄筛选条件.",
            "filters": {
                "rate_no": args.rate_no,
                "record_id": args.record_id,
                "carrier": args.carrier,
                "pod": args.pod,
                "pol": args.pol,
                "import_after": args.import_after,
                "import_before": args.import_before,
            },
        }, ensure_ascii=False, indent=2))
        sys.exit(3)

    # P0 保护: 预演模式
    if args.dry_run:
        print(json.dumps({
            "code": "ok",
            "ok": True,
            "dry_run": True,
            "record_ids": record_ids,
            "deleted_count": 0,
            "would_delete_count": len(record_ids),
            "message": f"[DRY-RUN] 将删除 {len(record_ids)} 条记录",
            "filters": {
                "rate_no": args.rate_no,
                "record_id": args.record_id,
                "carrier": args.carrier,
                "pod": args.pod,
                "pol": args.pol,
                "import_after": args.import_after,
                "import_before": args.import_before,
            },
        }, ensure_ascii=False, indent=2))
        sys.exit(0)

    # 实际删除
    if not args.yes:
        print(json.dumps({
            "code": "CONFIRM_REQUIRED",
            "error": "实际删除需要 --yes 标志",
            "record_ids": record_ids,
            "would_delete_count": len(record_ids),
        }, ensure_ascii=False, indent=2))
        sys.exit(2)

    deleted_ids = []
    failed_ids = []
    for rid in record_ids:
        rc, out, err = run_cmd([
            "lark-cli", "--as", "user", "base", "+record-delete",
            "--base-token", args.base_token,
            "--table-id", args.table_id,
            "--record-id", rid,
            "--yes",
        ])
        if rc == 0:
            try:
                data = json.loads(out)
                if data.get("ok"):
                    deleted_ids.append(rid)
                else:
                    failed_ids.append({"record_id": rid, "error": data.get("error", "unknown")})
            except Exception:
                deleted_ids.append(rid)
        else:
            failed_ids.append({"record_id": rid, "error": err or out})

    print(json.dumps({
        "code": "ok" if not failed_ids else "PARTIAL",
        "ok": len(failed_ids) == 0,
        "deleted_count": len(deleted_ids),
        "failed_count": len(failed_ids),
        "deleted_ids": deleted_ids,
        "failed": failed_ids,
        "filters": {
            "rate_no": args.rate_no,
            "record_id": args.record_id,
            "carrier": args.carrier,
            "pod": args.pod,
            "pol": args.pol,
            "import_after": args.import_after,
            "import_before": args.import_before,
        },
    }, ensure_ascii=False, indent=2))
    sys.exit(0 if not failed_ids else 1)


if __name__ == "__main__":
    main()
