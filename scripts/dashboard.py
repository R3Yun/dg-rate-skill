#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D25 统计看板 MVP (2026-08-03).

读取飞书 FCL 表 + 跑 quality_scan, 生成:
1. status distribution (生效/待补充)
2. expiry distribution (已过期/7天内/30天内/60天内/健康)
3. quality issues summary (从 quality_scan)
4. Feishu card JSON (Markdown 格式)

用法:
    dg-rate-query dashboard          # 文本输出 (table)
    dg-rate-query dashboard --json   # JSON 输出
    dg-rate-query dashboard --card   # Feishu 卡片 JSON
    dg-rate-query dashboard --send <chat_id>  # 推送到飞书
"""
import argparse
import datetime
import json
import sys
from typing import Any, Dict, List

from feishu_source import FeishuRateSource


def _today() -> datetime.date:
    return datetime.date.today()


def _status_distribution(records: List[Dict[str, Any]]) -> Dict[str, int]:
    """按 状态 字段分组计数. select 字段返回 list, 提取首个."""
    dist = {}
    for r in records:
        s = r.get("status") or r.get("状态") or "未知"
        if isinstance(s, dict):
            s = s.get("text", str(s))
        elif isinstance(s, list) and s:
            first = s[0]
            s = first.get("text", str(first)) if isinstance(first, dict) else str(first)
        dist[str(s)] = dist.get(str(s), 0) + 1
    return dist


def _expiry_distribution(records: List[Dict[str, Any]], today: datetime.date = None) -> Dict[str, int]:
    """按 有效期止 分桶: 已过期/7天/30天/60天/健康."""
    today = today or _today()
    dist = {"已过期": 0, "7天内": 0, "30天内": 0, "60天内": 0, "健康": 0}
    for r in records:
        vto = r.get("valid_to") or r.get("有效期止") or ""
        if not vto:
            continue
        try:
            d = datetime.date.fromisoformat(str(vto)[:10])
        except Exception:
            continue
        delta = (d - today).days
        if delta <= 0:
            dist["已过期"] += 1
        elif delta <= 7:
            dist["7天内"] += 1
        elif delta <= 30:
            dist["30天内"] += 1
        elif delta <= 60:
            dist["60天内"] += 1
        else:
            dist["健康"] += 1
    return dist


def _top_expiring(records: List[Dict[str, Any]], n: int = 5, today: datetime.date = None) -> List[Dict[str, Any]]:
    """快过期的 TOP N (按 valid_to 升序)."""
    today = today or _today()
    enriched = []
    for r in records:
        vto = r.get("valid_to") or r.get("有效期止") or ""
        if not vto:
            continue
        try:
            d = datetime.date.fromisoformat(str(vto)[:10])
        except Exception:
            continue
        delta = (d - today).days
        enriched.append({
            "pol": r.get("pol") or r.get("POL"),
            "pod": r.get("pod") or r.get("POD"),
            "carrier": r.get("carrier") or r.get("船公司"),
            "valid_to": str(vto)[:10],
            "days_left": delta,
        })
    enriched.sort(key=lambda x: x["days_left"])
    return enriched[:n]


def build_dashboard(
    records: List[Dict[str, Any]],
    quality_report: Dict[str, Any] = None,
    today: datetime.date = None,
) -> Dict[str, Any]:
    """构建看板数据.

    today: 可注入的"今天",默认 None 取真实当天. 测试可用固定日期避免过期漂移
    (D25 2026-08-03 写的硬编码 valid_to 在真实 today 下会跨桶).
    """
    today = today or _today()
    status_dist = _status_distribution(records)
    expiry_dist = _expiry_distribution(records, today)
    top_expiring = _top_expiring(records, n=5, today=today)
    return {
        "dashboard_time": today.isoformat(),
        "total_records": len(records),
        "status_distribution": status_dist,
        "expiry_distribution": expiry_dist,
        "top_expiring": top_expiring,
        "quality_issues": quality_report.get("total_issues", 0) if quality_report else None,
        "quality_summary": quality_report.get("summary", []) if quality_report else [],
    }


def generate_summary(dashboard: Dict[str, Any]) -> str:
    """数据驱动智能摘要 (类 LLM 风格输出).

    基于 dashboard 各项指标, 生成自然语言摘要.
    无需 LLM API, 完全本地计算.
    """
    total = dashboard.get("total_records", 0)
    if total == 0:
        return "看板: 暂无记录, 等待首批运价入库."
    lines = []
    expired = dashboard.get("expiry_distribution", {}).get("已过期", 0)
    within_7d = dashboard.get("expiry_distribution", {}).get("7天内", 0)
    within_30d = dashboard.get("expiry_distribution", {}).get("30天内", 0)
    healthy = dashboard.get("expiry_distribution", {}).get("健康", 0)
    pending = dashboard.get("status_distribution", {}).get("待补充", 0)
    active = dashboard.get("status_distribution", {}).get("已生效", 0)
    expired_pct = expired / total * 100 if total else 0
    active_pct = active / total * 100 if total else 0
    lines.append(
        f"📊 当前共 **{total}** 条运价, 已生效 {active} 条 ({active_pct:.0f}%), "
        f"待补充 {pending} 条."
    )
    if expired > 0:
        urgency = "🚨" if expired > total * 0.5 else "⚠️"
        lines.append(
            f"{urgency} **{expired}** 条已过期 ({expired_pct:.0f}%), "
            f"建议立即下架或续约."
        )
    if within_7d > 0:
        lines.append(
            f"🟠 **{within_7d}** 条 7 天内到期, 建议启动新一轮询价."
        )
    if within_30d > 0:
        lines.append(
            f"🟡 **{within_30d}** 条 30 天内到期, 提醒关注."
        )
    if healthy > 0:
        lines.append(
            f"🟢 **{healthy}** 条健康 (60 天以上), 短期无需关注."
        )
    top = dashboard.get("top_expiring", [])
    if top:
        carriers = sorted({r.get("carrier", "") for r in top if r.get("carrier")})
        if carriers:
            carrier_str = ", ".join(carriers[:3])
            more = "等" if len(carriers) > 3 else ""
            lines.append(
                f"📌 TOP 5 快过期运价集中在 {carrier_str} {more}"
            )
    q = dashboard.get("quality_issues")
    if q and q > 0:
        q_summary = dashboard.get("quality_summary", [])
        non_zero = [s["check"] for s in q_summary if s.get("count", 0) > 0][:3]
        if non_zero:
            lines.append(
                f"🔍 质量问题 {q} 条: " + ", ".join(non_zero) +
                (" 等" if len(non_zero) > 2 else "")
            )
    if expired == 0 and pending == 0 and within_7d == 0:
        lines.append("✅ 所有运价状态良好, 短期无需处理.")
    return "\n".join(lines)


def _fetch_records() -> List[Dict[str, Any]]:
    """从飞书读取所有记录. 把 {\"fields\": {...}} 嵌套结构摊平, 保留 record_id."""
    src = FeishuRateSource()
    raw = src.list_all_records(src.config["rate_table_id"], page_size=200)
    flat = []
    for r in raw:
        if not isinstance(r, dict):
            continue
        fields = r.get("fields", {}) or {}
        merged = dict(fields)
        if r.get("record_id"):
            merged["record_id"] = r["record_id"]
        if merged:
            flat.append(merged)
    return flat


def _fetch_quality_report(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """跑 quality_scan. 重定向 stdout 让 scan 的 print 不污染主输出."""
    import io
    import sys
    try:
        from quality_scan import scan
        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        try:
            result = scan(records)
        finally:
            sys.stdout = old_stdout
        return result
    except ImportError:
        return {"total_issues": 0, "summary": []}


def format_table(dashboard: Dict[str, Any]) -> str:
    """文本表格输出."""
    lines = []
    lines.append("=== 运价库统计看板 ===")
    lines.append(f"  日期: {dashboard['dashboard_time']}")
    lines.append(f"  总记录数: {dashboard['total_records']}")
    lines.append("")
    lines.append("--- 智能摘要 ---")
    lines.append(generate_summary(dashboard))
    lines.append("")
    lines.append("状态分布:")
    for s, c in sorted(dashboard["status_distribution"].items(), key=lambda x: -x[1]):
        lines.append(f"  {s}: {c}")
    lines.append("")
    lines.append("有效期分布:")
    for b, c in dashboard["expiry_distribution"].items():
        lines.append(f"  {b}: {c}")
    if dashboard.get("top_expiring"):
        lines.append("")
        lines.append("TOP 5 快过期:")
        for r in dashboard["top_expiring"]:
            lines.append(f"  {r['valid_to']} ({r['days_left']}天) {r.get('carrier', '')} {r.get('pol', '')}->{r.get('pod', '')}")
    if dashboard.get("quality_issues") is not None:
        lines.append("")
        lines.append(f"质量问题总数: {dashboard['quality_issues']}")
        for s in dashboard.get("quality_summary", []):
            if s.get("count", 0) > 0:
                lines.append(f"  {s['check']}: {s['count']}")
    return "\n".join(lines)


def format_card(dashboard: Dict[str, Any], use_llm: bool = False) -> str:
    """飞书 post 消息 Markdown 格式 (兼容 lark-cli 1.0.81 --markdown 标志).

    注: lark-cli 1.0.81 的 --content + --msg-type interactive 有 bug (230099 parse error).
    workaround: 用 --markdown (auto post 格式) 替代 interactive card.
    完整 interactive card 功能等 lark-cli 修复后再启用.

    use_llm=True 时调用 LLM 客户端 (需配置 D25_LLM_API_URL/KEY), fallback 到数据驱动.
    """
    lines = [f"**运价库看板 - {dashboard['dashboard_time']}**", ""]

    if use_llm:
        from llm_client import generate_summary_with_llm
        llm_summary = generate_summary_with_llm(dashboard, data_driven_fallback=generate_summary)
        lines.append(llm_summary)
    else:
        lines.append(generate_summary(dashboard))
    lines.append("")

    lines.append(f"**总记录数**: {dashboard['total_records']}")
    lines.append("")

    lines.append("**状态分布**")
    for s, c in sorted(dashboard["status_distribution"].items(), key=lambda x: -x[1]):
        lines.append(f"- {s}: {c}")
    lines.append("")

    lines.append("**有效期分布**")
    for b, c in dashboard["expiry_distribution"].items():
        lines.append(f"- {b}: {c}")
    lines.append("")

    if dashboard.get("top_expiring"):
        lines.append("**TOP 5 快过期**")
        for r in dashboard["top_expiring"]:
            sign = "+" if r["days_left"] >= 0 else ""
            lines.append(
                f"- `{r['valid_to']}` ({sign}{r['days_left']}天) "
                f"{r.get('carrier', '')} {r.get('pol', '')}->{r.get('pod', '')}"
            )
        lines.append("")

    if dashboard.get("quality_issues") is not None:
        qi = dashboard["quality_issues"]
        lines.append(f"**质量问题**: {qi} 条")
        for s in dashboard.get("quality_summary", []):
            if s.get("count", 0) > 0:
                lines.append(f"- {s['check']}: {s['count']}")
        lines.append("")

    lines.append(f"_(D25 看板 · 自动生成 · {dashboard['dashboard_time']})_")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--card", action="store_true", help="Feishu 卡片 JSON output")
    parser.add_argument("--send", metavar="CHAT_ID", help="推送到飞书 chat_id")
    parser.add_argument("--no-quality", action="store_true", help="跳过 quality_scan")
    parser.add_argument("--use-llm", action="store_true",
                        help="用 LLM 生成智能摘要 (需 D25_LLM_API_URL/KEY, 无配置时 fallback)")
    args = parser.parse_args()

    records = _fetch_records()
    quality_report = None if args.no_quality else _fetch_quality_report(records)
    dashboard = build_dashboard(records, quality_report)

    if args.card or args.send:
        print(format_card(dashboard, use_llm=args.use_llm))
        if args.json:
            print(json.dumps(dashboard, ensure_ascii=False, indent=2))
    elif args.json:
        print(json.dumps(dashboard, ensure_ascii=False, indent=2))
    else:
        print(format_table(dashboard))

    if args.send:
        import subprocess
        cmd = [
            "lark-cli", "--as", "user",
            "im", "+messages-send",
            "--chat-id", args.send,
            "--markdown", format_card(dashboard),
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            print(f"\n已推送到 {args.send}")
        else:
            print(f"\n推送失败: {r.stderr[:200]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())