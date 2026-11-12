#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""batch-write — D6 写类原子工具 (D6-9)

批量写入 FCL 海运费表. **D4 每条都校验 _provenance, 缺则全批拒写** (exit=3).

CLI:
  batch-write --records @entries.json

v3.7+: --import-user/--review-user 已废弃 (字段已删除), 保留为 no-op.
  batch-write --stdin < entries.json

JSON format:
  {"records":[
    {"POL":"CNSHA", "POD":"THBKK", ..., "_provenance":{"source_file":"x","parser":"read-xlsx"}},
    {"POL":"CNSHA", "POD":"SGSIN", ..., "_provenance":{"source_file":"x","parser":"read-xlsx"}}
  ]}
"""
import argparse
import json
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def validate_provenance(record):
    if "_provenance" not in record:
        return False, "缺少 _provenance (D4 决策)"
    prov = record["_provenance"]
    if not isinstance(prov, dict):
        return False, "_provenance 必须是对象"
    for k in ("source_file", "parser"):
        if k not in prov or not str(prov.get(k) or "").strip():
            return False, f"_provenance 缺必填字段 {k}"
    return True, ""


def detect_silent_failure(result, total_count, written, rejected, downgraded, record_ids):
    """D67-D (2026-08-10): 即使 result.success=True 也可能 record_ids 含 None 或
    written+rejected+downgraded != total, 强制识别为 silent failure.

    1v1 事件 #2 现场: batch_write 返回 ok:False, record_id_list:[None] 但
    lark error 字段为空, Python 端 result.success 可能仍为 True 但实际
    写入 0 条. 此函数用于 belt-and-suspenders 防御.

    Returns:
        None - 无静默失败
        str - 失败原因描述
    """
    if not getattr(result, "success", False):
        return None
    valid_record_ids = [rid for rid in (record_ids or []) if rid]
    if any(rid is None for rid in record_ids or []):
        return "record_ids 含 None 值"
    if written > 0 and not valid_record_ids:
        return f"written={written} 但 record_ids 为空"
    if total_count > 0 and (written + rejected + downgraded) != total_count:
        return (
            f"written({written}) + rejected({rejected}) + downgraded({downgraded})"
            f" != total({total_count})"
        )
    return None


def verify_written_records(writer, record_ids, sample_size=3):
    """P0 写后验证 (2026-08-12): 声称成功前, 抽查 record_get 确认 record_ids 真实存在.

    防"文本声称写入成功但实际 0 条"幻觉 (1v1 测试 3 轮全中招: task written_count=0
    但可可文本声称 N 条成功). 每批抽前 sample_size 条调 _record_get_status,
    任一查不到 → 返回缺失列表; 全部查到 → 返回 None.

    Args:
        writer: LarkRateWriter 实例 (需有 _record_get_status)
        record_ids: 写入返回的 record_id 列表
        sample_size: 抽查条数 (默认 3)

    Returns:
        None - 抽查全部真实存在
        list[str] - 抽查中 record_get 查不到的 record_id 列表
    """
    if not record_ids:
        return None
    sample = (record_ids or [])[:sample_size]
    missing = []
    for rid in sample:
        try:
            status = writer._record_get_status(rid)
        except Exception:
            status = None
        if status is None:
            missing.append(rid)
    return missing or None


def main():
    ap = argparse.ArgumentParser()
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--records", help="@file 或 JSON 字符串")
    src.add_argument("--stdin", action="store_true")
    ap.add_argument("--import-user", default=None)
    ap.add_argument("--review-user", default=None)
    ap.add_argument("--base-token", default="Eje8bWtVdaPPPosu0GQcPclQnut")
    ap.add_argument("--table-id", default="tblnCWVGvCfFHW6m")
    ap.add_argument("--status", default="已生效", choices=["待补充", "已生效"])
    ap.add_argument("--source-type", default="Excel导入")
    ap.add_argument("--source-url", default="")
    ap.add_argument("--source-path", default="")
    # P0-2026-07-23 修复: 默认拒绝 file/OCR/PDF source 写入, 需 --confirm-write 显式确认
    # 业务人员 @可可 含 "写入"/"入库" 指令时由可可自动传 --confirm-write
    ap.add_argument("--confirm-write", action="store_true",
                    help="Required for file/OCR/PDF source. LLM must confirm user intent before passing this.")
    ap.add_argument("--force", action="store_true",
                    help="Skip idempotency guard (use only for re-running after manual cleanup).")
    # D69 (2026-08-10): preview-only 模式 — parse + validate 不写入, 返回 JSON 预览供业务人员 review
    ap.add_argument("--preview-only", action="store_true",
                    help="Parse and validate but do NOT write to lark. Returns preview JSON for business user review.")
    # D16 P0-A: optional chat_id for STOP guard (raw CLI fallback path)
    ap.add_argument("--chat-id", default=None,
                    help="Optional. If provided, batch-write checks for user_stopped active task (D16 P0-A).")
    # A5 (WS-162, 2026-09-01): merge=true 模式 — 每条记录需带 record_id/rate_no,
    # 只更新提供字段 (merge_record), 不新建. 修复 WS-150 事件2: 可可带 record_id 调
    # batch_write 被误当新建 → 5 条重复.
    ap.add_argument("--merge", action="store_true",
                    help="Merge mode: each record must carry record_id (or rate_no); only provided fields are updated, no new records created.")
    args = ap.parse_args()

    # D16 P0-A: STOP guard — block batch-write if active task is user_stopped
    if args.chat_id:
        try:
            from task_state import RateTaskStore, is_user_stopped
            store = RateTaskStore()
            active = store.find_active(args.chat_id)
            if active and is_user_stopped(active):
                print(json.dumps({
                    "code": "USER_STOPPED",
                    "success": False,
                    "blocked_by": "P0-A (D16) task user_stopped / awaiting_user_confirmation",
                    "task_id": active.get("task_id"),
                    "pending_action": active.get("pending_action"),
                    "last_action": (active.get("execution") or {}).get("last_action"),
                    "message": "active task is marked user_stopped; refuse further write. Inspect task and ask business before resuming."
                }, ensure_ascii=False, indent=2))
                sys.exit(4)
        except ImportError:
            pass  # task_state module not in same dir; skip guard
        except Exception as _exc:
            print(json.dumps({"code": "STOP_GUARD_CHECK_FAILED", "error": str(_exc)}, ensure_ascii=False))
            sys.exit(4)

    if args.stdin:
        rec_str = sys.stdin.read().strip()
    elif args.records.startswith("@"):
        with open(args.records[1:], "r", encoding="utf-8") as f:
            rec_str = f.read().strip()
    else:
        rec_str = args.records

    try:
        payload = json.loads(rec_str)
    except Exception as e:
        print(json.dumps({"code": "INVALID_JSON", "error": str(e)}, ensure_ascii=False))
        sys.exit(2)

    if isinstance(payload, dict):
        records = payload.get("records") or payload.get("entries") or []
    elif isinstance(payload, list):
        records = payload
    else:
        print(json.dumps({"code": "INVALID_FORMAT", "error": "expected records array"}, ensure_ascii=False))
        sys.exit(2)

    if not records:
        print(json.dumps({"code": "EMPTY_BATCH", "error": "no records"}, ensure_ascii=False))
        sys.exit(2)

    # WS-151 事故 (2026-09-01): batch-write 是 INSERT-only, write_rates 不识别 record_id,
    # 带 record_id 的 payload 会被当新记录创建 → 11 条重复入库. 显式拒绝, 强制走
    # write-record --record-id <id> --record <json> --merge 更新路径.
    # A5 (WS-162, 2026-09-01): --merge 模式例外 — merge=true 时带 record_id 是合法更新
    # (走下方 merge_record 分支), 守卫只对 INSERT 路径 (无 --merge) 生效.
    # A5+ 复测修复 (2026-09-01, WS-162 打回): --preview-only 同样例外 — 预览只读不写库,
    # 不应在 preview 步拦截更新 payload (否则 preview_rendered 永远置不上 → batch_write
    # merge=true 死锁 PREVIEW_REQUIRED). 带 record_id 的预览走下方 merge-preview 分支.
    rid_records = [
        {"index": i, "record_id": r.get("record_id") or r.get("recordId")}
        for i, r in enumerate(records)
        if isinstance(r, dict) and (r.get("record_id") or r.get("recordId"))
    ]
    if rid_records and not args.merge and not args.preview_only:
        print(json.dumps({
            "code": "RECORD_ID_UPDATE_NOT_SUPPORTED",
            "success": False,
            "written": 0,
            "total": len(records),
            "record_ids": [r["record_id"] for r in rid_records],
            "error": "batch-write 仅支持 INSERT 新记录, 不支持 record_id 更新 (检测到 "
                     + str(len(rid_records)) + " 条带 record_id: "
                     + ", ".join(str(r["record_id"]) for r in rid_records[:5])
                     + ")。更新请用: write-record --record-id <id> --record <json> --merge "
                     + "或 write-record --carrier <船公司> --merge <json> 筛选批量更新。",
        }, ensure_ascii=False, indent=2))
        sys.exit(2)

    # merge / merge-preview (更新已有记录) 模式下 _provenance 可选 — 对齐 write-record.py
    # merge 语义: 更新路径不新建记录, 无溯源风险; INSERT 路径保持 D4 强校验不变.
    is_update_mode = bool(args.merge) or (args.preview_only and bool(rid_records))
    missing = []
    for i, r in enumerate(records):
        if is_update_mode:
            continue
        if isinstance(r, dict) and "fields" in r:
            ok, msg = validate_provenance(r)
        else:
            ok, msg = validate_provenance(r if isinstance(r, dict) else {})
        if not ok:
            missing.append({"index": i, "error": msg})

    if missing:
        print(json.dumps({
            "code": "MISSING_PROVENANCE",
            "success": False,
            "missing_records": missing,
            "total_records": len(records),
            "error": f"D4: {len(missing)}/{len(records)} 条记录缺 _provenance, 整批拒写"
        }, ensure_ascii=False, indent=2))
        sys.exit(3)

    entries = []
    for r in records:
        if isinstance(r, dict) and "fields" in r:
            # 保留顶层 record_id/recordId/rate_no (更新 payload 可能用 {record_id, fields:{}} 结构),
            # 驼峰 recordId 统一归一为 record_id; _provenance 缺省容忍 (merge/merge-preview 下可选).
            rid = r.get("record_id") or r.get("recordId")
            base = {}
            if rid:
                base["record_id"] = rid
            if r.get("rate_no"):
                base["rate_no"] = r["rate_no"]
            if "_provenance" in r:
                base["_provenance"] = r["_provenance"]
            entries.append({**base, **r["fields"]})
        elif isinstance(r, dict) and r.get("recordId"):
            # 扁平记录含驼峰 recordId → 归一为 record_id (merge/merge-preview 读取).
            base = {k: v for k, v in r.items() if k != "recordId"}
            base["record_id"] = r["recordId"]
            entries.append(base)
        else:
            entries.append(r)

    # A5+ 复测修复 (2026-09-01, WS-162 打回): merge 预览 — 渲染待更新目标记录与字段,
    # 只读绝不写库. 修 preview↔write 死锁: 无文件更新 task 必须先能预览 (置 preview_rendered),
    # 后续 batch_write(confirm_write=true, merge=true) 同 payload 才能过指纹+门禁整批更新.
    if args.preview_only and (args.merge or rid_records):
        preview_records = []
        preview_errors = []
        for i, rec in enumerate(entries):
            rid = str(rec.get("record_id", "") or "").strip()
            if not rid and rec.get("rate_no"):
                rid = f"rate_no:{rec['rate_no']}"  # 预览阶段不查库, 标注 rate_no 待写库时解析
            fields = {k: v for k, v in rec.items() if k not in ("record_id", "rate_no", "_provenance")}
            if not rid:
                preview_errors.append({"index": i, "error": "record_id (或 rate_no) 必填 — merge 模式不新建记录"})
            preview_records.append({"record_id": rid, "fields": fields})
        print(json.dumps({
            "code": "preview",
            "success": True,
            "preview": True,
            "mode": "merge",
            "total": len(entries),
            "records": preview_records,
            "errors": preview_errors,
            "message": "【无文件更新预览】merge preview-only: 内联 records 带 record_id 的整批更新 (无需文件、无需 parse_id), "
                       "渲染待更新目标记录与字段, NO write to lark. "
                       "业务确认后 batch_write(confirm_write=true, merge=true) 同 payload 写库。"
        }, ensure_ascii=False, indent=2))
        sys.exit(0)

    # A5 (WS-162): merge=true — 更新已有记录, 不新建
    if args.merge:
        try:
            from lark_rate_writer import LarkRateWriter as _MRW
            _mw = _MRW()
            merged_ids = []
            errors = []
            for i, rec in enumerate(entries):
                rid = str(rec.get("record_id", "") or "").strip()
                if not rid and rec.get("rate_no"):
                    from find_record_by_rate_no import find_record_by_rate_no
                    rid = find_record_by_rate_no(rec["rate_no"], base_token=args.base_token, table_id=args.table_id)
                if not rid:
                    errors.append({"index": i, "error": "record_id (或 rate_no) 必填 — merge 模式不新建记录"})
                    continue
                fields = {k: v for k, v in rec.items() if k not in ("record_id", "rate_no", "_provenance")}
                if not fields:
                    errors.append({"index": i, "error": "无待更新字段"})
                    continue
                try:
                    res = _mw.merge_record(rid, fields)
                except Exception as exc:
                    errors.append({"index": i, "error": str(exc)})
                    continue
                if res.success:
                    merged_ids.append(rid)
                else:
                    errors.append({"index": i, "record_id": rid, "error": res.error_msg})
            ok = len(merged_ids) == len(entries) and not errors
            print(json.dumps({
                "code": "ok" if ok else "MERGE_PARTIAL",
                "success": ok,
                "mode": "merge",
                "total": len(entries),
                "merged": len(merged_ids),
                "record_ids": merged_ids,
                "errors": errors,
                "message": f"merge 完成: {len(merged_ids)}/{len(entries)} 条更新, {len(errors)} 条失败" + ("" if ok else " (见 errors)")
            }, ensure_ascii=False, indent=2))
            sys.exit(0 if ok else 1)
        except Exception as e:
            print(json.dumps({"code": "MERGE_ERROR", "error": str(e), "trace": traceback.format_exc()[:500]}, ensure_ascii=False))
            sys.exit(1)

    # D69 (2026-08-10): preview-only 模式 — parse + validate + 报告但不写入
    if args.preview_only:
        try:
            from lark_rate_writer import LarkRateWriter as _PRW
            _pw = _PRW()
            preview = _pw.preview_records(entries, options={
                "status": args.status,
                "base_token": args.base_token,
                "table_id": args.table_id,
                "data_source": args.source_type,
                "source_url": args.source_url,
                "source_path": args.source_path,
            })
            print(json.dumps({
                "code": "preview",
                "success": True,
                "preview": True,
                "total": preview.get("total", len(entries)),
                "p0_count": preview.get("p0_count", 0),
                "p1_count": preview.get("p1_count", 0),
                "p2_count": preview.get("p2_count", 0),
                "records": preview.get("records", []),
                "abbreviations": preview.get("abbreviations", []),
                "dedupe_key": preview.get("dedupe_key"),
                "dedupe_status": preview.get("dedupe_status"),
                "message": "D69 preview-only: parse + validate done, NO write to lark. "
                           "Business user must review records + abbreviations, "
                           "then re-call with confirm_write=true to actually write."
            }, ensure_ascii=False, indent=2))
            sys.exit(0)
        except Exception as e:
            print(json.dumps({"code": "PREVIEW_ERROR", "error": str(e), "trace": traceback.format_exc()[:500]}, ensure_ascii=False))
            sys.exit(1)

    try:
        from lark_rate_writer import LarkRateWriter
        writer = LarkRateWriter()
        opts = {
            "status": args.status,
            "base_token": args.base_token,
            "table_id": args.table_id,
            "data_source": args.source_type,
            "source_url": args.source_url,
            "source_path": args.source_path,
            "confirm_write": args.confirm_write,  # P0-2026-07-23: file source 需 confirm
            "force": args.force,                  # P0-2026-07-23: skip dedupe guard
        }
        result = writer.write_rates(entries, options=opts)
        record_ids = list(getattr(result, "record_ids", []) or [])
        written = int(getattr(result, "write_count", 0) or 0)
        rejected = int(getattr(result, "rejected_count", 0) or 0)
        downgraded = int(getattr(result, "downgraded_count", 0) or 0)
        total_count = int(getattr(result, "total_count", len(entries)) or 0)
        p0_missing = int(getattr(result, "p0_missing_count", 0) or 0)
        successful_write = bool(
            result.success and written > 0 and len(record_ids) == written
        )
        silent_failure_reason = detect_silent_failure(
            result, total_count, written, rejected, downgraded, record_ids,
        )
        if silent_failure_reason:
            successful_write = False
            result.error_msg = f"lark silently failed: {silent_failure_reason}"

        # P0 写后验证 (2026-08-12): 声称成功前, 抽查 3 条 record_get 确认 record_ids 真实存在
        # 防"文本声称写入成功但实际 0 条"幻觉 (1v1 测试 3 轮全中招)
        verify_failed = None
        if successful_write and record_ids:
            missing = verify_written_records(writer, record_ids)
            if missing:
                verify_failed = missing
                successful_write = False
                result.error_msg = (
                    f"write reported ok but record_get cannot confirm {len(missing)} record_id(s): "
                    f"{missing}. Do NOT report success until verified."
                )
        code = (
            "CRITICAL_FIELDS_MISSING" if p0_missing > 0
            else "SILENT_FAILURE" if silent_failure_reason
            else "WRITE_VERIFY_FAILED" if verify_failed
            else "ok" if successful_write
            else "error"
        )
        schema_audit = result.schema_audit or {}
        source_upload = schema_audit.get("source_upload") or {}
        out = {
            "code": code,
            "success": successful_write,
            "total": int(getattr(result, "total_count", len(entries)) or 0),
            "written": written,
            "rejected": int(getattr(result, "rejected_count", 0) or 0),
            "downgraded": int(getattr(result, "downgraded_count", 0) or 0),
            "p0_missing": p0_missing,
            "p1_missing": int(getattr(result, "p1_missing_count", 0) or 0),
            "p2_missing": int(getattr(result, "p2_missing_count", 0) or 0),
            "missing_records": list(getattr(result, "missing_records", []) or []),
            "p1_missing_records": list(getattr(result, "p1_missing_records", []) or []),
            "p2_warnings": list(getattr(result, "p2_warnings", []) or []),
            "batch_no": ((result.schema_audit or {}).get("batch_record") or {}).get("batch_no"),
            "record_ids": record_ids,
            "source_upload": source_upload,
            "source_url": args.source_url or source_upload.get("share_url", ""),
            "error": result.error_msg,
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        sys.exit(0 if successful_write else 1)
    except Exception as e:
        print(json.dumps({"code": "WRITE_ERROR", "error": str(e), "trace": traceback.format_exc()[:500]}, ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    main()