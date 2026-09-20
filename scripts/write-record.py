#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""write-record — D6 写类原子工具 (D6-8)

单条写入或更新 FCL 海运费表. **D4 强制 _provenance, 缺则拒写** (exit=3 MISSING_PROVENANCE).

CLI:
  write-record --record entry.json                    # 新增
  write-record --record-id rec_xxx --merge entry.json # 更新

v3.7+: --import-user/--review-user 已废弃 (字段已删除), 保留为 no-op.
v3.10.7: 新增 --record-id + --merge 支持更新现有记录
"""
import argparse
import json
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def validate_provenance(record, is_merge=False):
    """验证 _provenance, merge 模式下可选"""
    if is_merge:
        # 更新模式下 _provenance 可选
        return True, ""
    if "_provenance" not in record:
        return False, "缺少 _provenance (D4 决策)"
    prov = record["_provenance"]
    if not isinstance(prov, dict):
        return False, "_provenance 必须是对象"
    for k in ("source_file", "parser"):
        if k not in prov or not str(prov.get(k) or "").strip():
            return False, f"_provenance 缺必填字段 {k}"
    return True, ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--record", required=True, help="@file 或 JSON 字符串")
    ap.add_argument("--record-id", default=None, help="飞书 record_id (rec_xxx), 用于更新现有记录")
    ap.add_argument("--merge", action="store_true", help="合并模式: 只更新提供的字段, 不覆盖其他字段")
    # v3.10.10: 筛选参数 (与 --record-id 互斥, 筛选后批量更新)
    ap.add_argument("--rate-no", default="", help="按运价编号筛选 (支持逗号分隔)")
    ap.add_argument("--carrier", default="", help="按船公司模糊匹配")
    ap.add_argument("--pod", default="", help="按目的港模糊匹配")
    ap.add_argument("--pol", default="", help="按起运港模糊匹配")
    ap.add_argument("--import-after", default="", help="按导入时间起始 (YYYY-MM-DD)")
    ap.add_argument("--import-before", default="", help="按导入时间截止 (YYYY-MM-DD)")
    ap.add_argument("--booking-agent", default="", help="按订舱代理精确匹配 (D93: 归一化去空白; 短名\"中外运\"/长法人名\"中外运集装箱运输有限公司\"均可直接查)")
    ap.add_argument("--import-user", default=None, help="(已废弃 v3.7) 不再写入飞书")
    ap.add_argument("--review-user", default=None, help="(已废弃 v3.7)")
    ap.add_argument("--base-token", default="Eje8bWtVdaPPPosu0GQcPclQnut")
    ap.add_argument("--table-id", default="tblnCWVGvCfFHW6m")
    ap.add_argument("--status", default="已生效", choices=["待补充", "已生效"])
    ap.add_argument("--source-type", default="文本聊天")
    ap.add_argument("--source-url", default="")
    ap.add_argument("--source-path", default="")
    args = ap.parse_args()

    rec_str = args.record
    if rec_str.startswith("@"):
        with open(rec_str[1:], "r", encoding="utf-8") as f:
            rec_str = f.read().strip()
    try:
        entry = json.loads(rec_str)
    except Exception as e:
        print(json.dumps({"code": "INVALID_JSON", "error": str(e)}, ensure_ascii=False))
        sys.exit(2)

    # v3.10.10: 筛选模式 - 通过筛选条件查找 record_ids, 批量更新
    has_filters = any([args.rate_no, args.carrier, args.pod, args.pol, args.import_after, args.import_before, args.booking_agent])
    if has_filters and not args.record_id:
        from find_records_filtered import find_records
        record_ids, err = find_records(
            base_token=args.base_token,
            table_id=args.table_id,
            rate_no=args.rate_no,
            carrier=args.carrier,
            pod=args.pod,
            pol=args.pol,
            import_after=args.import_after,
            import_before=args.import_before,
            booking_agent=args.booking_agent,
        )
        if err:
            print(json.dumps({"code": "FIND_ERROR", "error": err}, ensure_ascii=False))
            sys.exit(1)
        if not record_ids:
            print(json.dumps({"code": "NO_MATCH", "success": False, "message": "未找到符合条件的记录", "record_ids": []}, ensure_ascii=False))
            sys.exit(0)
        # 批量更新
        if isinstance(entry, dict) and "fields" in entry:
            update_fields = entry.get("fields", {})
        else:
            update_fields = entry
        update_fields.pop("_provenance", None)
        from lark_rate_writer import LarkRateWriter
        writer = LarkRateWriter()
        results = []
        for rid in record_ids:
            res = writer.merge_record(rid, update_fields) if args.merge else writer.update_record(rid, update_fields)
            results.append({"record_id": rid, "success": res.success, "error": res.error_msg})
        success_count = sum(1 for r in results if r["success"])
        print(json.dumps({
            "code": "ok" if success_count == len(results) else "PARTIAL",
            "success": success_count == len(results),
            "mode": "filter-batch-update",
            "total": len(record_ids),
            "written": success_count,
            "failed": len(results) - success_count,
            "record_ids": record_ids,
            "results": results,
        }, ensure_ascii=False, indent=2))
        sys.exit(0 if success_count == len(results) else 1)

    if isinstance(entry, dict) and "fields" in entry:
        record = entry.get("fields", {})
        prov = entry.get("_provenance") or record.pop("_provenance", None)
        if prov:
            record["_provenance"] = prov
    else:
        record = entry

    is_merge = bool(args.record_id) or args.merge
    ok, msg = validate_provenance(record, is_merge=is_merge)
    if not ok:
        print(json.dumps({"code": "MISSING_PROVENANCE", "success": False, "error": msg}, ensure_ascii=False))
        sys.exit(3)

    # P0 保护 (2026-08-12): merge/update 模式禁止清空 P0 关键字段
    # 防修正过程把 POL/POD/有效期等关键字段误改为空 (P0 写库闸门同源规则)
    P0_FIELDS = ("pol", "pod", "pc", "valid_from", "valid_to")
    p0_emptied = [f for f in P0_FIELDS if f in record and not str(record.get(f) or "").strip()]
    if p0_emptied and (args.record_id or args.merge):
        print(json.dumps({
            "code": "P0_FIELD_EMPTY",
            "success": False,
            "written": 0,
            "error": f"禁止将 P0 关键字段清空: {p0_emptied}. POL/POD/P/C/有效期起止不能为空, 请提供有效值.",
        }, ensure_ascii=False, indent=2))
        sys.exit(4)

    try:
        from lark_rate_writer import LarkRateWriter
        writer = LarkRateWriter()

        # 更新模式
        if args.record_id:
            # 移除 _provenance 字段 (只用于内部日志, 不能写入飞书)
            record.pop('_provenance', None)
            
            if not args.merge:
                # update 模式: 完全覆盖指定字段
                result = writer.update_record(args.record_id, record)
            else:
                # merge 模式: 只更新提供的字段
                result = writer.merge_record(args.record_id, record)

            record_ids = [args.record_id]
            written = 1 if result.success else 0
            out = {
                "code": "ok" if result.success else "error",
                "success": result.success,
                "total": 1,
                "written": written,
                "rejected": 0,
                "downgraded": 0,
                "p0_missing": 0,
                "p1_missing": 0,
                "p2_missing": 0,
                "missing_records": [],
                "p1_missing_records": [],
                "p2_warnings": [],
                "record_id": args.record_id,
                "record_ids": record_ids,
                "batch_no": None,
                "error": result.error_msg,
                "mode": "merge" if args.merge else "update",
            }
            print(json.dumps(out, ensure_ascii=False, indent=2))
            sys.exit(0 if result.success else 1)

        # 新增模式 (原有逻辑)
        opts = {
            "status": args.status,
            "base_token": args.base_token,
            "table_id": args.table_id,
            "data_source": args.source_type,
            "source_url": args.source_url,
            "source_path": args.source_path,
        }
        result = writer.write_rates([record], options=opts)
        record_ids = list(getattr(result, "record_ids", []) or [])
        written = int(getattr(result, "write_count", 0) or 0)
        p0_missing = int(getattr(result, "p0_missing_count", 0) or 0)
        successful_write = bool(
            result.success and written == 1 and len(record_ids) == 1
        )
        code = (
            "CRITICAL_FIELDS_MISSING" if p0_missing > 0
            else "ok" if successful_write
            else "error"
        )
        out = {
            "code": code,
            "success": successful_write,
            "total": int(getattr(result, "total_count", 1) or 0),
            "written": written,
            "rejected": int(getattr(result, "rejected_count", 0) or 0),
            "downgraded": int(getattr(result, "downgraded_count", 0) or 0),
            "p0_missing": p0_missing,
            "p1_missing": int(getattr(result, "p1_missing_count", 0) or 0),
            "p2_missing": int(getattr(result, "p2_missing_count", 0) or 0),
            "missing_records": list(getattr(result, "missing_records", []) or []),
            "p1_missing_records": list(getattr(result, "p1_missing_records", []) or []),
            "p2_warnings": list(getattr(result, "p2_warnings", []) or []),
            "record_id": record_ids[0] if record_ids else None,
            "record_ids": record_ids,
            "batch_no": ((result.schema_audit or {}).get("batch_record") or {}).get("batch_no"),
            "error": result.error_msg,
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        sys.exit(0 if successful_write else 1)
    except Exception as e:
        print(json.dumps({"code": "WRITE_ERROR", "error": str(e), "trace": traceback.format_exc()[:500]}, ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    main()
