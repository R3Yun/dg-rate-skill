# -*- coding: utf-8 -*-

"""
5 级运价到期提醒

拉所有状态=生效中 的运价（含已过期的），按 valid_to 距今天的天数分桶：
- 已过期  (remaining <= 0)
- 1-7天   (极紧急, 立即下架或续约)
- 8-14天  (紧急, 启动新一轮询价)
- 15-30天 (警告, 提醒关注)
- 31-60天 (提示, 关注即可)
- >60天   (健康)

输出：
1. stdout 表格 + JSON 汇总
2. Lark 卡片 JSON（可被 lark-cli 直接发到指定 chat）
3. 可选 --send 模式：自动 lark-cli 发到指定 chat_id

运行：
    python3 expiry_reminder.py                       # 打印汇总
    python3 expiry_reminder.py --json                # JSON 输出
    python3 expiry_reminder.py --lark-card           # Lark 卡片 JSON
    python3 expiry_reminder.py --send oc_xxx         # 推到指定群
"""

import argparse
import datetime
import json
import sys
from typing import List, Dict, Any
from feishu_source import fetch_rates_from_feishu


# 5 级分桶定义（按 remaining_days 范围）
BUCKETS = [
    ("已过期",     -9999,   0),
    ("1-7天",       1,    7),
    ("8-14天",      8,   14),
    ("15-30天",    15,   30),
    ("31-60天",    31,   60),
    (">60天",      61, 99999),
]


def _to_date(s):
    """YYYY-MM-DD -> date, 容错返回 None"""
    if not s:
        return None
    s = str(s).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            return datetime.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def bucket_of(remaining_days):
    for name, lo, hi in BUCKETS:
        if lo <= remaining_days <= hi:
            return name
    return "未知"


def _entry_remaining_days(e, today):
    v = _to_date(getattr(e, "valid_to", ""))
    if not v:
        return None
    return (v - today).days


def build_summary(entries, today=None):
    """分类汇总。每条记录带 remaining_days + bucket。"""
    if today is None:
        today = datetime.date.today()
    items = []
    for e in entries:
        rd = _entry_remaining_days(e, today)
        items.append({
            "record_id": getattr(e, "_record_id", ""),
            "pol": getattr(e, "pol", ""),
            "pod": getattr(e, "pod", ""),
            "carrier": getattr(e, "carrier", ""),
            "of_20": getattr(e, "of_20", None),
            "of_40hq": getattr(e, "of_40hq", None),
            "valid_from": getattr(e, "valid_from", ""),
            "valid_to": getattr(e, "valid_to", ""),
            "remaining_days": rd,
            "bucket": bucket_of(rd) if rd is not None else "无有效期",
            "remark": getattr(e, "remark", "")[:80],
        })
    return items, today


def aggregate(items):
    """按 bucket 汇总条数 + 总 O/F 价值。"""
    out = {name: {"count": 0, "rate_count": 0} for name, _, _ in BUCKETS}
    out["无有效期"] = {"count": 0, "rate_count": 0}
    for it in items:
        b = it["bucket"]
        out[b]["count"] += 1
        if it["of_20"]:
            out[b]["rate_count"] += 1
    return out


def render_table(items, today):
    """生成易读的文本表格（仅展示 14天内 + 已过期）。"""
    action = ["已过期", "1-7天", "8-14天"]
    rows = [it for it in items if it["bucket"] in action]
    if not rows:
        return "今天 " + today.isoformat() + " 没有 14天内 / 已过期 的运价。"
    lines = []
    lines.append("到期临近运价列表 (" + today.isoformat() + " 截止):")
    lines.append("")
    lines.append("剩余天数 | 航线               | 船公司 | 20GP  | 40HQ  | 有效期止     | 备注")
    lines.append("-" * 100)
    for it in sorted(rows, key=lambda x: (x["remaining_days"] if x["remaining_days"] is not None else 9999)):
        rd = str(it["remaining_days"]) if it["remaining_days"] is not None else "?"
        of20 = str(it["of_20"]) if it["of_20"] else "-"
        of40 = str(it["of_40hq"]) if it["of_40hq"] else "-"
        lines.append(
            "  " + rd.rjust(6) + "  | "
            + (it["pol"] + "->" + it["pod"]).ljust(18) + " | "
            + it["carrier"].ljust(6) + " | "
            + of20.rjust(6) + " | "
            + of40.rjust(6) + " | "
            + it["valid_to"].ljust(12) + " | "
            + it["remark"][:30]
        )
    return chr(10).join(lines)


def render_lark_card(items, agg, today):
    """生成飞书消息卡片 JSON。"""
    urgent = [it for it in items if it["bucket"] in ("已过期", "1-7天", "8-14天")]
    summary_line = " ".join([
        b[0] + ":" + str(agg[b[0]]["count"])
        for b in BUCKETS if agg[b[0]]["count"] > 0
    ])
    # 顶部 banner
    if agg["已过期"]["count"] > 0:
        title = "[!] " + str(agg["已过期"]["count"]) + " 条运价已过期，需立即下架"
        template = "red"
    elif agg["1-7天"]["count"] > 0:
        title = "[!!] " + str(agg["1-7天"]["count"]) + " 条运价 7天内到期"
        template = "orange"
    elif agg["8-14天"]["count"] > 0:
        title = "[!] " + str(agg["8-14天"]["count"]) + " 条运价 14天内到期"
        template = "yellow"
    else:
        title = "[OK] 所有已生效运价都在 14天以上"
        template = "green"
    elements = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "**到期临近运价** (" + today.isoformat() + " 截止)\n" + summary_line,
            },
        },
        {"tag": "hr"},
    ]
    for it in urgent[:10]:  # 最多展示 10 条
        rd = it["remaining_days"] if it["remaining_days"] is not None else "?"
        of20 = str(it["of_20"]) if it["of_20"] else "-"
        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    "[" + it["bucket"] + "] "
                    + "**" + it["pol"] + "->" + it["pod"] + "**"
                    + " (" + it["carrier"] + ") "
                    + "20GP=" + of20
                    + " | " + str(rd) + "天"
                    + " | 止:" + it["valid_to"],
                ),
            },
        })
    if len(urgent) > 10:
        elements.append({
            "tag": "note",
            "text": {
                "tag": "plain_text",
                "content": "...还有 " + str(len(urgent) - 10) + " 条, 详见终端输出或全量 JSON",
            },
        })
    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "template": template,
                "title": {
                    "tag": "plain_text",
                    "content": title,
                },
            },
            "elements": elements,
        },
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", default="已生效")
    ap.add_argument("--include-all-status", action="store_true", help="包括 待补充 等所有状态")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--lark-card", action="store_true")
    ap.add_argument("--send", metavar="CHAT_ID", help="推到指定 chat_id（lark-cli）")
    ap.add_argument("--quiet", action="store_true", help="只打 JSON/Lark，不打表格")
    args = ap.parse_args()

    if args.include_all_status:
        # 把所有状态都拉一遍，需要分多次调用
        statuses = ["已生效", "待补充"]
        all_entries = []
        for s in statuses:
            es, _ = fetch_rates_from_feishu(status_filter=s, include_dg=False, only_valid=False)
            all_entries.extend(es)
        entries = all_entries
    else:
        entries, _ = fetch_rates_from_feishu(status_filter=args.status, include_dg=False, only_valid=False)

    items, today = build_summary(entries)
    agg = aggregate(items)

    if args.json:
        print(json.dumps({"today": today.isoformat(), "summary": agg, "items": items}, ensure_ascii=False, indent=2))
        return

    if args.lark_card:
        card = render_lark_card(items, agg, today)
        print(json.dumps(card, ensure_ascii=False, indent=2))
        return

    if args.send:
        card = render_lark_card(items, agg, today)
        import paramiko
        # 通过 NAS SSH 推到 OpenClaw-coco 内的 lark-cli
        from feishu_source import DEFAULT_CONFIG
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect(DEFAULT_CONFIG["nas_host"], port=DEFAULT_CONFIG.get("nas_port", 2122),
                  username=DEFAULT_CONFIG["nas_user"], password=DEFAULT_CONFIG["nas_password"], timeout=15)
        cmd = (
            "sudo docker exec -u node " + DEFAULT_CONFIG["container_name"]
            + " lark-cli im +messages-send --chat-id " + args.send
            + " --interactive-card " + chr(39) + json.dumps(card, ensure_ascii=False) + chr(39)
        )
        si, so, se = c.exec_command(cmd, timeout=30)
        print(so.read().decode("utf-8", errors="replace"))
        print("[STDERR]", se.read().decode("utf-8", errors="replace"))
        c.close()
        return

    # 默认模式：表格 + 摘要
    print(render_table(items, today))
    print("")
    print("汇总 (" + today.isoformat() + " 截止, 状态=" + args.status + "):")
    for name, _, _ in BUCKETS:
        print("  " + name.ljust(8) + " : " + str(agg[name]["count"]) + " 条")
    if agg["无有效期"]["count"] > 0:
        print("  无有效期    : " + str(agg["无有效期"]["count"]) + " 条")


if __name__ == "__main__":
    main()