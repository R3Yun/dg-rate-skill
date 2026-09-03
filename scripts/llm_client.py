#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D25 Phase 3: LLM 智能摘要客户端 (2026-08-04, revised).

使用可可智能体 (OpenClaw agent) 作为 LLM provider, 而非独立 LLM API.

架构:
  dashboard -> llm_client.generate_summary_with_llm() -> 可可 (via OpenClaw)
  可可用其内置 LLM (M2.7) 分析 dashboard 数据并生成摘要.

优势:
  - 零额外 LLM API 密钥 (重用可可的 LLM)
  - 可可有项目上下文, 摘要更相关
  - 失败时自动 fallback 到数据驱动摘要 (generate_summary)

用法:
    from llm_client import generate_summary_with_llm
    text = generate_summary_with_llm(dashboard_data)
    # 可可不可用时自动 fallback 到数据驱动
"""
import json
import os
import subprocess
import time
from typing import Any, Dict, Optional


_PROMPT_TEMPLATE = """你是运价库运营分析师 (可可). 基于以下数据, 用简洁中文生成 1-3 句关键洞察.

数据:
- 总记录: {total} 条
- 状态: {active} 条已生效, {pending} 条待补充
- 有效期: {expired} 条已过期, {within_7d} 条 7天内到期, {within_30d} 条 30天内到期, {healthy} 条健康
- 质量问题: {quality_issues} 条, 主要类型: {quality_checks}
- 快过期 TOP 5: {top_expiring}

要求:
- 重点: 紧急程度 (已过期 > 7天内 > 30天内)
- 风格: 数据驱动 + emoji
- 长度: 不超过 5 行
- 结尾: 给出 1 条建议
"""


def _build_prompt(dashboard: Dict[str, Any]) -> str:
    """从 dashboard 数据构建 prompt."""
    status_dist = dashboard.get("status_distribution", {})
    expiry_dist = dashboard.get("expiry_distribution", {})
    top = dashboard.get("top_expiring", [])
    top_str = ", ".join(
        f"{r.get('carrier', '?')}/{r.get('pol', '?')}-{r.get('pod', '?')} ({r.get('days_left', '?')}d)"
        for r in top[:5]
    ) or "无"
    quality_summary = dashboard.get("quality_summary", [])
    non_zero = [s["check"] for s in quality_summary if s.get("count", 0) > 0][:3]
    return _PROMPT_TEMPLATE.format(
        total=dashboard.get("total_records", 0),
        active=status_dist.get("已生效", 0),
        pending=status_dist.get("待补充", 0),
        expired=expiry_dist.get("已过期", 0),
        within_7d=expiry_dist.get("7天内", 0),
        within_30d=expiry_dist.get("30天内", 0),
        healthy=expiry_dist.get("健康", 0),
        quality_issues=dashboard.get("quality_issues", 0) or 0,
        quality_checks="、".join(non_zero) or "无",
        top_expiring=top_str,
    )


def _call_koko_via_openclaw(prompt: str, timeout: int = 30) -> Optional[str]:
    """通过 OpenClaw gateway 调用可可生成回复.

    Returns:
        可可的回复文本, 失败返回 None.
    """
    cmd = [
        "openclaw", "agent", "chat",
        "--agent", "main",
        "--message", prompt,
        "--format", "text",
    ]
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
        return None
    return None


def _call_koko_via_http(prompt: str, timeout: int = 30) -> Optional[str]:
    """通过 HTTP gateway 调用可可 (fallback 方式).

    OpenClaw gateway 默认在 ws://127.0.0.1:18789 (HTTP 端口相同).
    """
    import urllib.request
    import urllib.error
    gateway_url = os.environ.get("OPENCLAW_GATEWAY_URL", "http://127.0.0.1:18789")
    payload = json.dumps({
        "agent": "main",
        "message": prompt,
        "stream": False,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{gateway_url}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("response") or data.get("message")
    except (urllib.error.URLError, urllib.error.HTTPError, Exception):
        return None


def generate_summary_with_llm(
    dashboard: Dict[str, Any],
    data_driven_fallback: Any = None,
) -> str:
    """生成 LLM 智能摘要 (用可可作为 LLM provider).

    Args:
        dashboard: build_dashboard() 输出的数据
        data_driven_fallback: 可可不可用时的 fallback 函数 (默认 generate_summary)

    Returns:
        可可生成的摘要文本, 或 fallback 输出
    """
    import time
    prompt = _build_prompt(dashboard)
    start = time.time()
    result = _call_koko_via_openclaw(prompt)
    if result is None:
        result = _call_koko_via_http(prompt)
    if result:
        elapsed = time.time() - start
        return f"_(可可生成, {elapsed:.1f}s)_\n\n{result}"
    if data_driven_fallback is not None:
        result_text = data_driven_fallback(dashboard)
        elapsed = time.time() - start
        return result_text + f"\n\n_(可可不可用 ({elapsed:.1f}s), 使用数据驱动 fallback)_"
    return ""
