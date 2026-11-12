#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
update_status.py - 批量更新飞书多维表格记录的状态

用法:
  dg-rate-query update-status --record-ids rec1,rec2 --status 已生效

  # FCL 状态只表示数据可用性：
  - 待补充 -> 已生效
  - 已生效 -> 待补充

实现:
  1. 优先本地调 lark-cli (PATH 上能找到 lark)
  2. 否则打 NDJSON 让 OpenClaw Agent 自己调
"""
import sys
import os
import argparse
import json
import shutil
import subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# 2026-07-12 修复: coco 容器 PATH 不含 /home/node/.openclaw/workspace/bin,
# 导致 shutil.which("lark-cli") 永远返回 None, 走 NDJSON 兜底不真更新
def _find_lark_bin():
    """查找 lark wrapper binary, 兼容多种部署位置."""
    import os
    # 1. PATH 上 (开发机/deploy/start.sh 安装)
    found = shutil.which("lark-cli")
    if found:
        return found
    # 2. coco 容器 workspace bin (OpenClaw 标配)
    candidates = ["lark-cli"]
    for c in candidates:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None

VALID_STATUSES = ("待补充", "已生效")
VALID_TRANSITIONS = {
    "待补充": ["已生效"],
    "已生效": ["待补充"],
}

def call_lark_cli(base_token, table_id, record_ids, patch, fmt="json"):
    """调 lark base +record-batch-update. record_id_list + patch 格式.

    lark-cli 限制: 同一个 patch 应用到所有记录, 最多 200 条/次.
    """
    lark_bin = _find_lark_bin()
    if not lark_bin:
        return None
    payload = {
        "record_id_list": record_ids,
        "patch": patch,
    }
    cmd = [
        lark_bin, "base", "+record-batch-update",
        "--base-token", base_token,
        "--table-id", table_id,
        "--json", json.dumps(payload, ensure_ascii=False),
        "--format", fmt,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return {
            "returncode": r.returncode,
            "stdout": r.stdout.strip(),
            "stderr": r.stderr.strip(),
        }
    except Exception as e:
        return {"returncode": -1, "stderr": str(e)}

def build_patch(target_status, reviewer=""):
    """v3.7+: 审核人字段已删除, 只更新状态."""
    return {"状态": target_status}

def main():
    ap = argparse.ArgumentParser(description="批量更新记录状态 (v3.7+ 无审核人)")
    ap.add_argument("--record-ids", required=True, help="逗号分隔的 record_id 列表")
    ap.add_argument("--status", required=True, choices=list(VALID_STATUSES),
                    help="目标状态")
    ap.add_argument("--review-user", default="", help="(已废弃 v3.7) 状态转换不再需要审核人")
    ap.add_argument("--base-token", default="Eje8bWtVdaPPPosu0GQcPclQnut")
    ap.add_argument("--table-id", default="tblnCWVGvCfFHW6m")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    record_ids = [r.strip() for r in args.record_ids.split(",") if r.strip()]
    target_status = args.status

    print(f"目标记录: {len(record_ids)} 条")
    print(f"目标状态: {target_status}")
    # v3.7+: 审核人字段已删除, 不再展示

    patch = build_patch(target_status)
    print("")
    print("=== 更新计划 ===")
    print(f"每条记录 patch: {json.dumps(patch, ensure_ascii=False)}")

    if args.dry_run:
        print("[DRY-RUN] 不执行实际更新")
        return 0

    # 超过 200 条要分批
    BATCH = 200
    if _find_lark_bin():
        print("")
        print("=== 调 lark-cli record-batch-update ===")
        all_ok = True
        for i in range(0, len(record_ids), BATCH):
            batch = record_ids[i:i + BATCH]
            print(f"批次 {i//BATCH + 1}: {len(batch)} 条")
            result = call_lark_cli(args.base_token, args.table_id, batch, patch)
            if result and result["returncode"] == 0:
                print("[OK]", result["stdout"][:300])
            else:
                print("[FAIL]")
                print(json.dumps(result, ensure_ascii=False, indent=2))
                all_ok = False
        return 0 if all_ok else 1
    else:
        print("")
        print("=== NDJSON 兜底 (PATH 上没有 lark) ===")
        print("请在 OpenClaw 容器内执行以下 lark-cli 命令:")
        payload = json.dumps(
            {"record_id_list": record_ids, "patch": patch},
            ensure_ascii=False,
        )
        lark_bin = _find_lark_bin() or "lark"
        print(
            f"  {lark_bin} base +record-batch-update "
            f"--base-token {args.base_token} "
            f"--table-id {args.table_id} "
            f"--json '" + payload + "' "
            f"--format json"
        )
        return 0

if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except ValueError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(2)