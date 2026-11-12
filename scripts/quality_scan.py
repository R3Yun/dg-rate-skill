# -*- coding: utf-8 -*-
"""
飞书运价库数据质量扫描器

扫描 FCL海运费表 全部记录，输出 6 类问题清单：
1. POL 空
2. 船公司 空
3. 有效期已过期
4. 有效期起 > 有效期止（数据错误）
5. DG 价格小数点不统一
6. 导入人 / 审核人 空
"""
import sys
import os
import json
import datetime
import re
from typing import Dict, Any, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from feishu_source import FeishuRateSource


POL_KEY = "POL"
POD_KEY = "POD"
CARRIER_KEY = "\u8239\u516c\u53f8"
REMARK_KEY = "\u5907\u6ce8"
STATUS_KEY = "\u72b6\u6001"
VALID_FROM_KEY = "\u6709\u6548\u671f\u8d77"
VALID_TO_KEY = "\u6709\u6548\u671f\u6b62"
REVIEWER_KEY = "\u5ba1\u6838\u4eba"
STATUS_ACTIVE = "\u751f\u6548\u4e2d"

PC_KEY = "P/C"
# "来源文件" 字段已删 (2026-07-16 v3.2); 不再扫描此项
DATA_SOURCE_KEY = "数据来源"
PRICE_KEYS = ("20GP O/F(USD)", "40GP O/F(USD)", "40HQ O/F(USD)")



def _fields(r):
    """兼容嵌套 ({record_id, fields}) 与摊平 (顶层字段) 两种记录结构."""
    if not isinstance(r, dict):
        return {}
    f = r.get("fields")
    return f if isinstance(f, dict) else r


def _first(v):
    if v is None or v == "":
        return None
    if isinstance(v, list):
        return v[0] if v else None
    return str(v)


def check_pol_empty(records):
    issues = []
    for r in records:
        f = _fields(r)
        pol = f.get(POL_KEY, None)
        if pol is None or pol == "" or pol == []:
            issues.append({
                "record_id": r["record_id"],
                "issue": "POL \u7a7a",
                "detail": "\u8d77\u8fd0\u6e2f\u7f3a\u5931",
                "POD": f.get(POD_KEY),
                "carrier": _first(f.get(CARRIER_KEY)),
            })
    return issues


def check_carrier_empty(records):
    issues = []
    for r in records:
        f = _fields(r)
        carrier = f.get(CARRIER_KEY, None)
        if carrier is None or carrier == "" or carrier == []:
            remark = f.get(REMARK_KEY, "") or ""
            m = re.search(r"\u8239\u516c\u53f8[:\uff1a=]\s*([A-Z][A-Z0-9/\-_.&]{1,30})", remark)
            hint = m.group(1) if m else ""
            issues.append({
                "record_id": r["record_id"],
                "issue": "\u8239\u516c\u53f8 \u7a7a",
                "detail": ("\u5907\u6ce8\u91cc\u6709\u63d0\u793a: " + hint) if hint else "\u65e0\u5907\u6ce8\u63d0\u793a",
                "POD": f.get(POD_KEY),
                "POL": f.get(POL_KEY),
            })
    return issues


def check_expired(records, today=None):
    today = today or datetime.date.today().strftime("%Y-%m-%d")
    issues = []
    for r in records:
        f = _fields(r)
        vt = _first(f.get(VALID_TO_KEY))
        if vt and vt < today:
            issues.append({
                "record_id": r["record_id"],
                "issue": "\u5df2\u8fc7\u671f",
                "detail": "\u6709\u6548\u671f\u6b62 " + str(vt) + " < " + today,
                "POD": f.get(POD_KEY),
                "POL": f.get(POL_KEY),
            })
    return issues


def check_date_range(records):
    issues = []
    for r in records:
        f = _fields(r)
        vf = _first(f.get(VALID_FROM_KEY))
        vt = _first(f.get(VALID_TO_KEY))
        if vf and vt and vf > vt:
            issues.append({
                "record_id": r["record_id"],
                "issue": "\u6709\u6548\u671f\u9519\u8bef",
                "detail": "\u8d77 " + str(vf) + " > \u6b62 " + str(vt),
                "POD": f.get(POD_KEY),
                "POL": f.get(POL_KEY),
            })
    return issues


def check_dg_decimal(records):
    pattern = re.compile(r"DG(\d+\.?\d*)/(\d+\.?\d*)")
    issues = []
    for r in records:
        f = _fields(r)
        remark = f.get(REMARK_KEY, "") or ""
        for m in pattern.finditer(remark):
            a, b = m.group(1), m.group(2)
            if "." in a or "." in b:
                issues.append({
                    "record_id": r["record_id"],
                    "issue": "DG \u5c0f\u6570\u70b9\u4e0d\u7edf\u4e00",
                    "detail": "DG" + a + "/" + b,
                    "POD": f.get(POD_KEY),
                    "POL": f.get(POL_KEY),
                })
                break
    return issues


def check_record_completeness(records):
    """写入后门禁：检查业务必填、来源、人员和至少一个价格字段。"""
    issues = []
    required = {
        POL_KEY: "POL",
        POD_KEY: "POD",
        PC_KEY: "P/C",
        VALID_FROM_KEY: "有效期起",
        VALID_TO_KEY: "有效期止",
        # v3.7+: 导入人字段已删除
        STATUS_KEY: "状态",
        DATA_SOURCE_KEY: "数据来源",
        # SOURCE_FILE_KEY 字段已删, 此项检查移除
    }
    for record in records:
        fields = _fields(record)
        missing = [label for key, label in required.items() if not _first(fields.get(key))]
        prices = [_first(fields.get(key)) for key in PRICE_KEYS]
        if not any(value not in (None, "", "0", 0) for value in prices):
            missing.append("20GP/40GP/40HQ 至少一个价格")
        pod = _first(fields.get(POD_KEY))
        if pod and not re.fullmatch(r"[A-Z]{5}", str(pod)):
            missing.append("POD 必须为英文 5 位 UN/LOCODE")
        if missing:
            issues.append({
                "record_id": record.get("record_id", ""),
                "issue": "入库完整性",
                "detail": "缺失或不合规: " + ", ".join(missing),
                "POL": fields.get(POL_KEY),
                "POD": fields.get(POD_KEY),
                "missing": missing,
            })
    return issues




CHECKS = [
    ("入库完整性", check_record_completeness),
    ("POL \u7a7a", check_pol_empty),
    ("\u8239\u516c\u53f8 \u7a7a", check_carrier_empty),
    ("\u5df2\u8fc7\u671f", check_expired),
    ("\u6709\u6548\u671f\u9519\u8bef", check_date_range),
    ("DG \u5c0f\u6570\u70b9\u4e0d\u7edf\u4e00", check_dg_decimal),
]


def scan(records=None):
    if records is None:
        src = FeishuRateSource()
        records = src.list_all_records(src.config["rate_table_id"], page_size=200)
    print("\u626b\u63cf\u603b\u6761\u6570\uff1a" + str(len(records)))
    summary = []
    for name, fn in CHECKS:
        issues = fn(records)
        summary.append({"check": name, "count": len(issues), "issues": issues})
        print("  " + name + ": " + str(len(issues)) + " \u6761")
    return {
        "scan_time": datetime.datetime.now().isoformat(timespec="seconds"),
        "total_records": len(records),
        "summary": summary,
        "total_issues": sum(s["count"] for s in summary),
    }


def format_markdown(report):
    lines = []
    lines.append("# \u8fd0\u4ef7\u5e93\u6570\u636e\u8d28\u91cf\u626b\u63cf\u62a5\u544a")
    lines.append("")
    lines.append("- \u626b\u63cf\u65f6\u95f4\uff1a" + report["scan_time"])
    lines.append("- \u603b\u6761\u6570\uff1a**" + str(report["total_records"]) + "**")
    lines.append("- \u95ee\u9898\u603b\u6570\uff1a**" + str(report["total_issues"]) + "**")
    lines.append("")
    lines.append("## \u95ee\u9898\u5206\u5e03")
    lines.append("")
    lines.append("| \u68c0\u67e5\u9879 | \u95ee\u9898\u6570 |")
    lines.append("|------|--------|")
    for s in report["summary"]:
        lines.append("| " + s["check"] + " | " + str(s["count"]) + " |")
    lines.append("")
    lines.append("## \u8be6\u7ec6\u95ee\u9898")
    for s in report["summary"]:
        if not s["issues"]:
            continue
        lines.append("")
        lines.append("### " + s["check"] + " (" + str(s["count"]) + " \u6761)")
        lines.append("")
        lines.append("| record_id | POL | POD | \u8be6\u60c5 |")
        lines.append("|-----------|-----|-----|--------|")
        for it in s["issues"][:30]:
            lines.append("| " + it.get("record_id", "") + " | " + str(it.get("POL", "") or "") + " | " + str(it.get("POD", "") or "") + " | " + it.get("detail", "") + " |")
        if len(s["issues"]) > 30:
            lines.append("| ... | ... | ... | \u8fd8\u6709 " + str(len(s["issues"]) - 30) + " \u6761 |")
    return "\n".join(lines)


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--output", default="")
    args = ap.parse_args()
    report = scan()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    md = format_markdown(report)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(md)
        print("已写入 " + args.output)
        return
    try:
        print(md)
    except UnicodeEncodeError:
        fallback = "_scan_output.md"
        with open(fallback, "w", encoding="utf-8") as f:
            f.write(md)
        print("控制台编码不支持，写入 " + fallback)
if __name__ == "__main__":
    main()
