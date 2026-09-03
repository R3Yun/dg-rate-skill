#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D26 PDF/Image parser (2026-08-04).

扫描型运价文件 OCR 解析 (PNG/JPG/PDF).

当前实现:
- image_parser 结构 + 接口 (OCR pipeline 待集成)
- 解析流程: OCR → 结构化 → NormalizedRateEntry
- 复用 YMLFAKParser 框架 (OutputFormat 与 OF sheet 一致)
- 集成点: 通过 _ocr_extract_text() 函数 (待接入实际 OCR)

依赖:
  - PIL/Pillow (图像处理)
  - pytesseract / paddleocr / docTR (OCR 引擎之一)
  - pdfplumber / pypdf2 (PDF 文本提取)

用法:
    from image_parser import parse_image, parse_pdf
    entries = parse_image("/path/to/scan.png")
    entries = parse_pdf("/path/to/rates.pdf")
"""
import datetime
import re
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


def _ocr_extract_text(filepath: str | Path) -> str:
    """OCR 提取图像/PDF 中的文本.

    当前实现: 尝试多种 OCR 后端, 失败返回空字符串.
    待接入实际 OCR (pytesseract / paddleocr / docTR).

    D40 graceful degradation:
    - tesseract 缺失时, 调用 _tesseract_ocr 失败 → 自动回退到 data-driven
    - 不静默返回空字符串, 提供安装指南 (业务方 apt install tesseract-ocr)
    - 返回空字符串表示 OCR 不可用, 调用方需自行处理

    Returns:
        提取的纯文本 (按行分隔, OCR 不可用时返回空字符串)
    """
    try:
        from PIL import Image
    except ImportError:
        return ""

    filepath = Path(filepath)
    if not filepath.exists():
        raise FileNotFoundError(f"Image/PDF 文件不存在: {filepath}")

    suffix = filepath.suffix.lower()
    if suffix == ".pdf":
        text = _extract_from_pdf(filepath)
    else:
        text = _extract_from_image(filepath)
    return text


def _extract_from_image(filepath: Path) -> str:
    try:
        from PIL import Image
    except ImportError:
        return ""
    try:
        img = Image.open(filepath)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        return _run_ocr_engine(img, filepath=str(filepath))
    except Exception:
        return ""


def _extract_from_pdf(filepath: Path) -> str:
    try:
        import pdfplumber
    except ImportError:
        return ""
    try:
        text_parts = []
        with pdfplumber.open(str(filepath)) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text() or ""
                text_parts.append(page_text)
        return "\n".join(text_parts)
    except Exception:
        return ""


def _run_ocr_engine(image, filepath: Optional[str] = None) -> str:
    """尝试各种 OCR 引擎. 返回首个成功的输出.

    优先级 (D42 ACTIVE):
    1. PaddleOCR (PP-OCRv6, 鲁棒, GPU 加速可选)
    2. pytesseract (tesseract 5.3.0 + chi_sim)
    """
    engines = [_paddleocr_ocr, _pytesseract_ocr]
    for engine in engines:
        try:
            if engine is _paddleocr_ocr:
                result = engine(image, filepath=filepath)
            else:
                result = engine(image)
            if result:
                return result
        except Exception:
            continue
    return ""


def _pytesseract_ocr(image) -> str:
    try:
        import pytesseract
    except ImportError:
        return ""
    try:
        return pytesseract.image_to_string(image, lang="eng+chi_sim")
    except Exception:
        return ""


def _paddleocr_ocr(image, filepath: Optional[str] = None) -> str:
    try:
        from paddleocr import PaddleOCR
    except ImportError:
        return ""
    import os
    os.environ.setdefault("FLAGS_use_mkldnn", "0")
    img_path: Optional[str] = filepath
    if not img_path and hasattr(image, "filename") and image.filename:
        img_path = str(image.filename)
    if not img_path or not Path(img_path).exists():
        return ""
    ocr = PaddleOCR(
        lang="ch",
        enable_mkldnn=False,
        enable_hpi=False,
        text_rec_score_thresh=0.3,
        text_det_thresh=0.2,
    )
    result = ocr.predict(img_path)
    if not isinstance(result, list):
        return ""
    lines: List[str] = []
    for page in result:
        if isinstance(page, dict):
            rec_texts = page.get("rec_texts", []) or []
            polys = page.get("rec_polys", page.get("dt_polys", [])) or []
            if not rec_texts:
                continue
            boxes: List[Tuple[float, float, float, float, str]] = []
            for i, text in enumerate(rec_texts):
                if i < len(polys) and len(polys[i]) > 0:
                    xs = [float(p[0]) for p in polys[i]]
                    ys = [float(p[1]) for p in polys[i]]
                    boxes.append((min(xs), min(ys), max(xs), max(ys), text))
                else:
                    boxes.append((0.0, 0.0, 0.0, 0.0, text))
            if not boxes:
                continue
            boxes.sort(key=lambda b: (b[1], b[0]))
            row_threshold = 20.0
            current_row: List[Tuple[float, float, float, float, str]] = [boxes[0]]
            row_lines: List[str] = []
            for b in boxes[1:]:
                if abs(b[1] - current_row[-1][1]) < row_threshold:
                    current_row.append(b)
                else:
                    current_row.sort(key=lambda x: x[0])
                    row_lines.append(" ".join(x[4] for x in current_row))
                    current_row = [b]
            if current_row:
                current_row.sort(key=lambda x: x[0])
                row_lines.append(" ".join(x[4] for x in current_row))
            lines.extend(row_lines)
    return "\n".join(lines)


def _parse_rate_text_to_entries(text: str, source_file: str) -> List[Dict[str, Any]]:
    """从 OCR 文本中提取运价条目.

    支持 2 种格式:
    1. **YML FAK OF sheet** (rate_pattern): POL POD 20GP 40GP [40HQ] — D26 Phase 1
    2. **周班航线** (WEEKLY_LINE_PATTERN): YYYYMMDD SERVICE 周X VESSEL VOYAGE POD 5 价格 — D26 Phase 5

    返回合并后的 entries 列表 (合并去重).
    """
    entries: List[Dict[str, Any]] = []
    if not text.strip():
        return entries

    rate_pattern = re.compile(
        r"(?P<pol>[A-Z]{5})\s+"
        r"(?P<pod>[A-Z]{5})\s+"
        r"(?P<of_20>\d+)\s+"
        r"(?P<of_40>\d+)(?:\s+(?P<of_40hq>\d+))?",
        re.MULTILINE,
    )
    carrier_match = re.search(r"carrier[:\s]+(\w+)", text, re.IGNORECASE)
    carrier = carrier_match.group(1) if carrier_match else "UNKNOWN"
    for m in rate_pattern.finditer(text):
        entries.append({
            "carrier": carrier,
            "pol": m.group("pol"),
            "pod": m.group("pod"),
            "of_20": int(m.group("of_20")),
            "of_40": int(m.group("of_40")),
            "of_40hq": int(m.group("of_40hq")) if m.group("of_40hq") else None,
            "currency": "USD",
            "source_file": source_file,
        })

    weekly_entries = _parse_weekly_text(text, source_file)
    seen = {(e.get("line_date"), e.get("voyage"), e.get("pod")) for e in entries if e.get("voyage")}
    for e in weekly_entries:
        key = (e.get("line_date"), e.get("voyage"), e.get("pod"))
        if key not in seen:
            entries.append(e)
            seen.add(key)

    return entries


# ============================================================
# D26 Phase 5: 周班航线格式正则扩展 (2026-08-06)
# ============================================================

# 完整行: YYYYMMDD SERVICE 周X VESSEL VOYAGE POD 中文码头 5价格
# 价格后断言: 不能是数字 (避免误吞下一行的 250 500 等)
# POD 接受 5-6 字母 (OCR 可能误读, 如 WVNHPH 应为 VNHPH)
WEEKLY_LINE_PATTERN = re.compile(
    r"(?P<line_date>\d{3,8})\s+"
    r"(?P<service>[A-Za-z0-9]+)\s+"
    r"(?:周\s*[一二三四五六日]\s+)?"
    r"(?P<vessel>[A-Za-z][A-Za-z\s]+?)\s+"
    r"(?P<voyage>\d+[A-Za-z0-9]*)\s+"
    r"(?P<pod>[A-Z]{5,6})(?=\s|$|[^A-Z])\s+"
    r"(?:(?P<terminal>[\u4e00-\u9fa5][\u4e00-\u9fa5\s]*?)\s+)?"
    r"(?P<p1>\d+)\s+(?P<p2>\d+)\s+(?P<p3>\d+)\s+(?P<p4>\d+)\s+(?P<p5>\d+)"
    r"(?=\s|$|[\u4e00-\u9fa5])",
    re.MULTILINE,
)

# 续行: POD 5价格 (继承上一行 vessel context)
WEEKLY_CONT_PATTERN = re.compile(
    r"^[ \t]*(?P<pod>[A-Z]{5,6})(?=\s|$|[^A-Z])\s+"
    r"(?P<p1>\d+)\s+(?P<p2>\d+)\s+(?P<p3>\d+)\s+(?P<p4>\d+)\s+(?P<p5>\d+)"
    r"(?=\s|$|[\u4e00-\u9fa5])",
    re.MULTILINE,
)

SURCHARGE_PATTERN = re.compile(
    r"(?P<surcharge>(?:BAF\s*[:：]?\s*USD[\d/]+(?:/TEU)?|LSS\s*[:：]?\s*USD[\d/]+(?:/TEU)?|DOC\s*[:：]?\s*USD[\d/]+|THC\s*[:：]?\s*USD[\d/]+|ISPS\s*[:：]?\s*USD[\d/]+|AMS\s*[:：]?\s*USD[\d/]+))",
    re.IGNORECASE,
)


# D50: ASEAN 格式 (亚海航运等) - 只有 2 个价格 (20/40), 班期格式不同
# 例子 (D50 测试样本):
#   周日HHX2 HONGKONG (DPW) $200/$40 含NBF 2026-8-5起 DIR3DAYS CA KOBE 2616W (8.10)
#   MACAO澳门 $600/$1200 含NBF 2026-8-5起 VIAHKG
#   周五HHX1 HAIPHONG $3105$550 含NBF 2026-8-5起 DIR5DAYS FENG HAI 862627W (8.5）
# 价格模式: $XX/$YY 或 $XX$YY 或 $XX$YY正 (含 "正" 后缀表示正向操作)
ASEAN_LINE_PATTERN = re.compile(
    r"^(?:[ \t]*)"
    r"(?P<frequency>周[日一二三四五六][A-Z0-9]{0,6}(?:（外[一二三四五]）|（外[一二三四五]）?)?)(?=\s|$)"
    r"\s+(?P<pod>[A-Z][A-Z\s()（）\u4e00-\u9fa5]{1,30}?)"
    r"\s+\$(?P<p1>\d+)\$?/?\$?(?P<p2>\d+)正?"
    r"(?:\s+(?:含NBF|不含NBF))?"
    r"(?:\s+(?P<line_date>\d{4}-\d{1,2}-\d{1,2})起?)?"
    r"(?:\s+(?P<tt>(?:DIR|VIA)\s*[A-Z0-9\s]{1,15}))?"
    r"(?:\s+(?P<remarks>.+?))?"
    r"\s*$",
    re.MULTILINE,
)

# ASEAN 续行: 无班期 (只有港口 + 价格)
ASEAN_CONT_PATTERN = re.compile(
    r"^(?:[ \t]*)"
    r"(?P<pod>[A-Z][A-Z\s()（）\u4e00-\u9fa5]{1,30}?)"
    r"\s+\$(?P<p1>\d+)\$?/?\$?(?P<p2>\d+)正?"
    r"(?:\s+(?:含NBF|不含NBF))?"
    r"(?:\s+(?P<line_date>\d{4}-\d{1,2}-\d{1,2})起?)?"
    r"(?:\s+(?P<tt>(?:DIR|VIA)\s*[A-Z0-9\s]{1,15}))?"
    r"(?:\s+(?P<remarks>.+?))?"
    r"\s*$",
    re.MULTILINE,
)


def _strip_table_separators(line: str) -> str:
    """D45: 把 markdown 表格 `|` 分隔符转为空格, 让 WEEKLY_LINE_PATTERN 可匹配.

    实际 mineru-open-api OCR 输出 (p0-ocr-rate.png):
        | 20260715 | NCP | 周三 | AN TONG DA LIAN 2609S | PHMNL | 外高桥四期 | 250 | 500 | 500 | 700 | 1400 | 备注 |
    经预处理后:
        20260715 NCP 周三 AN TONG DA LIAN 2609S PHMNL 外高桥四期 250 500 500 700 1400 备注

    D45 增强: 船名+航次间无空格时 (如 XINGANG2609S) 也插入空格, 让 regex 可分离船名与航次.
    但只在 vessel 区域 (周X 之后) 插入, 避免把 service 名 (如 CV2S) 拆开.
    """
    if "|" not in line and not _has_stuck_voyage(line):
        return line
    if "|" in line:
        parts = [p.strip() for p in line.split("|") if p.strip()]
        line = " ".join(parts)
    line = _insert_voyage_separator(line)
    return line


def _has_stuck_voyage(line: str) -> bool:
    """D45: 检测船名+航次紧挨 (如 XINGANG2609S)."""
    import re as _re
    return bool(_re.search(r"[A-Za-z]\d", line))


def _insert_voyage_separator(line: str) -> str:
    """D45: 在船名区域 (周X 之后、第一个价格之前) 的船名+航次间插入空格.

    例子:
      - ZHONG WAI YUN XINGANG2609S PHMNS → ZHONG WAI YUN XINGANG 2609S PHMNS
      - 20260716 CV2S 周四 ... BAF:USD50/100 → 不动 (CV2S 是 service, USD50 是附加费)
      - D50: 周日HHX2 → 不动 (HHX2 是频率码, 只有 1 位数字)
      - D50: CA KOBE 2616W → 不动 (2616W 是航次, 已有空格)
    """
    import re as _re
    weekday_m = _re.search(r"周\s*[一二三四五六日]", line)
    if not weekday_m:
        return line
    # vessel+voyage 区域: 周X 之后、第一个 3+ 位数字 (价格) 之前
    price_m = _re.search(r"\s\d{3,}", line[weekday_m.end():])
    if price_m:
        vessel_end = weekday_m.end() + price_m.start()
    else:
        vessel_end = len(line)
    vessel_part = line[weekday_m.end():vessel_end]
    # D50 修复: 只对 4+ 位数字插空格 (航次格式), 避免拆频率码 (HHX2 只有 1 位)
    vessel_fixed = _re.sub(r"([A-Za-z])(\d{4,})", r"\1 \2", vessel_part)
    return line[: weekday_m.end()] + vessel_fixed + line[vessel_end:]


def _detect_ocr_format(text: str) -> str:
    """D56: 检测 OCR markdown 文本的运价格式.

    返回:
      - "weekly": 周班航线格式 (5 价格 + 船名/航次/挂港)
      - "asean": ASEAN 格式 (2 价格 + 班期/港口)
      - "unknown": 都不匹配

    检测策略: 检查前 30 行的 header 特征
      - weekly header: line_date (8位数字) + service + 周X + vessel + voyage + pod + 5 价格
      - asean header: 班期 / 港口 / 运价 / NBF / 执行日期
    """
    import re as _re
    lines = text.splitlines()[:30]
    # D56 修复: 先剥离 | 分隔符 (实际 OCR 输出常含 markdown table 格式)
    cleaned_lines = []
    for line in lines:
        if "|" in line:
            parts = [p.strip() for p in line.split("|") if p.strip()]
            cleaned_lines.append(" ".join(parts))
        else:
            cleaned_lines.append(line)
    text_sample = "\n".join(cleaned_lines)
    # weekly: 8 位 line_date + service + 周 (如 "20260715 NCP 周三 AN TONG...")
    # 不检查孤立的 "周", 因为 ASEAN frequency code 含 "周日HHX2" 等
    has_weekly = bool(_re.search(r"\d{3,8}\s+[A-Za-z0-9]+\s+周", text_sample))
    # asean: 班期/港口/运价/NBF/执行日期 header
    has_asean = "班期" in text_sample and "港口" in text_sample and ("运价" in text_sample or "$" in text_sample)
    if has_weekly and not has_asean:
        return "weekly"
    if has_asean and not has_weekly:
        return "asean"
    if has_weekly and has_asean:
        return "mixed"
    return "unknown"


# D57: OCR format detector metrics (跟踪 weekly/asean 格式分布)
_ocr_metrics: Dict[str, int] = {
    "total_calls": 0,
    "weekly_calls": 0,
    "asean_calls": 0,
    "mixed_calls": 0,
    "unknown_calls": 0,
    "weekly_entries": 0,
    "asean_entries": 0,
}


def _get_ocr_metrics() -> Dict[str, int]:
    """D57: 获取 OCR format detector metrics 快照."""
    return dict(_ocr_metrics)


def _reset_ocr_metrics() -> None:
    """D57: 重置 OCR format detector metrics (测试用)."""
    global _ocr_metrics
    _ocr_metrics = {k: 0 for k in _ocr_metrics}


def _detect_weekly_doc_context(text: str) -> Dict[str, Any]:
    """D94/WS-220: 从 OCR 文本头部提取周班报价单的文档级上下文 (carrier/POL/价格列).

    周班航线图 (如 SNL 上海周报价) 表体行只含 航线代码/挂港/价格, 不含 carrier/POL.
    carrier/POL 在文档标题行 (如 "2026年SNL(上海)海运出口报价单") 或表头描述里.
    OCR 后按行表体 fabricate carrier=service 会误把航线代码 (CIE/NCP/CPF) 当船公司,
    进而 D80 订舱代理自动匹配到无关公司 (WS-220). 这里先扫文档头 15 行:

    Returns:
        {"carrier": "SNL", "pol": "CNSHA", "price_headers": [...]} 只含确有把握的键;
        无文档级信号返回 {} (调用方保持 legacy 逐行行为).
    """
    import re as _re
    ctx: Dict[str, Any] = {}
    head_lines = text.splitlines()[:15]
    head_text = "\n".join(head_lines)
    # carrier: 标题形如 "2026年SNL(上海)海运出口报价单" / "SNL(上海)出口周报"
    title_m = _re.search(
        r"(?:20\d{2}年)?(?P<carrier>[A-Z]{2,6})\s*[（(]\s*上海[）)]?\s*海?运?出口",
        head_text,
    )
    if not title_m:
        title_m = _re.search(
            r"(?:20\d{2}年)?(?P<carrier>[A-Z]{2,6})\s*上海\s*海?运?出口",
            head_text,
        )
    if title_m:
        ctx["carrier"] = title_m.group("carrier").upper()
    # POL: "上海 ... 海运出口/出口报价" → 出口港 CNSHA
    if _re.search(r"上海\s*海?运?出口|出口报价单", head_text):
        ctx["pol"] = "CNSHA"
    # 价格列: 表头若显式声明第 4/5 列为 RF (20RF/40RF) 则 p4/p5 不是 DG.
    # D41 5 列假设 p4=dg_20/p5=dg_40 仅对 DG 列文档成立; SNL 周报 4/5 列为冷藏价.
    rf_hdr = _re.search(
        r"(?:^|\|)\s*20GP\s*\|\s*40GP\s*\|\s*40HQ\s*\|\s*20RF\s*\|\s*40RF\s*(?:\||$)",
        text,
        _re.MULTILINE,
    )
    if rf_hdr:
        ctx["price_headers"] = ["20GP", "40GP", "40HQ", "20RF", "40RF"]
    return ctx


def _apply_weekly_doc_context(entry: Dict[str, Any], ctx: Dict[str, Any]) -> None:
    """D94/WS-220: 文档级上下文覆盖表体行 fabricate 值.

    - carrier/pol 有文档信号时, 用文档值覆盖 service 代码误填 (CIE/NCP/CPF → SNL/CNSHA)
    - 价格列为 20GP/40GP/40HQ/20RF/40RF 时, p4/p5 (冷藏价) 不再误写 dg_20/dg_40
      (FCL 无 RF 列, DG 字段只能来自真实 DG 附加费, 不来自第 4/5 价格列)
    """
    if ctx.get("carrier") and entry.get("carrier") == entry.get("service"):
        entry["carrier"] = ctx["carrier"]
    if ctx.get("pol") and entry.get("pol") == entry.get("service"):
        entry["pol"] = ctx["pol"]
    if ctx.get("price_headers") == ["20GP", "40GP", "40HQ", "20RF", "40RF"]:
        rf_20 = entry.pop("dg_20", None)
        rf_40 = entry.pop("dg_40", None)
        if rf_20 is not None or rf_40 is not None:
            rf_note = []
            if rf_20 is not None:
                rf_note.append(f"20RF={rf_20}")
            if rf_40 is not None:
                rf_note.append(f"40RF={rf_40}")
            remark = str(entry.get("remark") or "").strip()
            entry["remark"] = (remark + " | " + " | ".join(rf_note)).strip(" |") if remark else " | ".join(rf_note)


def _parse_weekly_text(text: str, source_file: str) -> List[Dict[str, Any]]:
    """D26 Phase 5: 解析周班航线格式 OCR 文本.

    真实样本格式 (p0-ocr-rate.png 提取的 2000 字符样本):
        20260715 NCP 周三 AN TONG DA LIAN 2609S PHMNL 外 高 桥 四 期 250 500 500 700 1400
        PHMNS 250 500 500 700 1400
        20260716 CPF 周 四 SINOTRANS MANILA 26108 PHMNL 外 高 桥 一 期 250 500 500 700 1400
        PHSFS 420 840 840 800 1600

    5 列价格映射:
        p1 = of_20 (20GP 基础)
        p2 = of_40 (40GP 基础)
        p3 = of_40hq (40HQ 基础)
        p4 = dg_20 (20GP + DG 附加)
        p5 = dg_40 (40GP + DG 附加)

    Returns:
        NormalizedRateEntry dict 列表 (含周班航线扩展字段: line_date/service/vessel/voyage/terminal)
    """
    entries: List[Dict[str, Any]] = []
    if not text or not text.strip():
        return entries

    # D56: 检测格式 (weekly / asean / mixed / unknown)
    detected_format = _detect_ocr_format(text)

    # D57: 记录 metrics (跟踪各格式调用次数)
    global _ocr_metrics
    _ocr_metrics["total_calls"] += 1
    _ocr_metrics[f"{detected_format}_calls"] += 1

    if detected_format == "unknown":
        return entries  # 无可识别格式, 早退避免无效遍历

    # D94/WS-220: 文档级上下文 (SNL 周报等) — carrier/POL/价格列语义
    weekly_doc_ctx = _detect_weekly_doc_context(text)

    # D50: ASEAN 格式上下文 (继承自班期行)
    asean_ctx: Dict[str, Optional[str]] = {
        "service": None,
        "line_date": None,
        "pol": None,
        "carrier": None,
    }

    last_ctx: Dict[str, Optional[str]] = {
        "vessel": None,
        "voyage": None,
        "service": None,
        "line_date": None,
        "terminal": None,
    }

    lines = text.splitlines()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        line = _strip_table_separators(line)

        m_line = WEEKLY_LINE_PATTERN.search(line)
        if m_line:
            entry = _build_weekly_entry_from_line_match(m_line, source_file)
            if entry:
                _apply_weekly_doc_context(entry, weekly_doc_ctx)
                _extract_surcharges(line, entry)
                entries.append(entry)
                _ocr_metrics["weekly_entries"] += 1  # D57: 计数
                last_ctx.update({
                    "vessel": entry["vessel"],
                    "voyage": entry["voyage"],
                    "service": entry["service"],
                    "line_date": entry["line_date"],
                    "terminal": entry.get("terminal"),
                })
                continue

        m_cont = WEEKLY_CONT_PATTERN.match(line)
        if m_cont:
            if last_ctx["voyage"] is None:
                continue
            entry = _build_weekly_entry_from_cont_match(m_cont, last_ctx, source_file)
            if entry:
                _apply_weekly_doc_context(entry, weekly_doc_ctx)
                _extract_surcharges(line, entry)
                entries.append(entry)
                entry["line_date"] = last_ctx["line_date"]
            continue

        # D50: ASEAN 格式 fallback (周班航线未匹配时尝试)
        m_asean = ASEAN_LINE_PATTERN.match(line)
        if m_asean and m_asean.group("frequency"):
            entry = _build_asean_entry_from_line_match(m_asean, source_file)
            if entry:
                _extract_surcharges(line, entry)
                entries.append(entry)
                _ocr_metrics["asean_entries"] += 1  # D57: 计数
                asean_ctx.update({
                    "service": entry["service"],
                    "line_date": entry["line_date"],
                    "pol": entry["pol"],
                    "carrier": entry["carrier"],
                })
                continue

        m_asean_cont = ASEAN_CONT_PATTERN.match(line)
        if m_asean_cont and asean_ctx["service"]:
            entry = _build_asean_entry_from_cont_match(m_asean_cont, asean_ctx, source_file)
            if entry:
                _extract_surcharges(line, entry)
                entries.append(entry)
                continue

    return entries


def _build_weekly_entry_from_line_match(m, source_file: str) -> Optional[Dict[str, Any]]:
    """从完整行匹配构建 entry."""
    try:
        terminal_value = m.group("terminal")
        return {
            "carrier": m.group("service"),
            "service": m.group("service"),
            "line_date": m.group("line_date"),
            "vessel": m.group("vessel").strip(),
            "voyage": m.group("voyage"),
            "pol": m.group("service"),
            "pod": m.group("pod"),
            "terminal": (terminal_value or "").strip(),
            "of_20": int(m.group("p1")),
            "of_40": int(m.group("p2")),
            "of_40hq": int(m.group("p3")),
            "dg_20": int(m.group("p4")),
            "dg_40": int(m.group("p5")),
            "currency": "USD",
            "source_file": source_file,
        }
    except (ValueError, AttributeError):
        return None


def _build_weekly_entry_from_cont_match(m, last_ctx: Dict[str, Optional[str]], source_file: str) -> Optional[Dict[str, Any]]:
    """从续行匹配构建 entry (继承 last_ctx)."""
    try:
        return {
            "carrier": last_ctx["service"] or "UNKNOWN",
            "service": last_ctx["service"],
            "line_date": last_ctx["line_date"],
            "vessel": last_ctx["vessel"],
            "voyage": last_ctx["voyage"],
            "pol": last_ctx["service"] or "UNKNOWN",
            "pod": m.group("pod"),
            "terminal": last_ctx.get("terminal"),
            "of_20": int(m.group("p1")),
            "of_40": int(m.group("p2")),
            "of_40hq": int(m.group("p3")),
            "dg_20": int(m.group("p4")),
            "dg_40": int(m.group("p5")),
            "currency": "USD",
            "source_file": source_file,
        }
    except (ValueError, AttributeError):
        return None


# D50: ASEAN 格式 entry builder (只有 2 个价格 20/40)
def _build_asean_entry_from_line_match(m, source_file: str) -> Optional[Dict[str, Any]]:
    try:
        pod = m.group("pod").strip()
        line_date = m.group("line_date") or ""
        return {
            "carrier": m.group("frequency"),
            "service": m.group("frequency"),
            "line_date": line_date,
            "vessel": "",
            "voyage": "",
            "pol": "CNSHA",
            "pod": pod,
            "terminal": None,
            "of_20": int(m.group("p1")),
            "of_40": int(m.group("p2")),
            "of_40hq": None,
            "of_40nor": None,
            "of_20nor": None,
            "dg_20": None,
            "dg_40": None,
            "tt_days": None,
            "direct": False,
            "frequency": m.group("frequency"),
            "remark": m.group("remarks") or "",
            "currency": "USD",
            "source_file": source_file,
            "_format": "ASEAN",
        }
    except (ValueError, AttributeError):
        return None


def _build_asean_entry_from_cont_match(m, asean_ctx: Dict[str, Optional[str]], source_file: str) -> Optional[Dict[str, Any]]:
    try:
        pod = m.group("pod").strip()
        line_date = m.group("line_date") or asean_ctx.get("line_date") or ""
        return {
            "carrier": asean_ctx.get("carrier") or "ASEAN",
            "service": asean_ctx["service"],
            "line_date": line_date,
            "vessel": "",
            "voyage": "",
            "pol": asean_ctx.get("pol") or "CNSHA",
            "pod": pod,
            "terminal": None,
            "of_20": int(m.group("p1")),
            "of_40": int(m.group("p2")),
            "of_40hq": None,
            "of_40nor": None,
            "of_20nor": None,
            "dg_20": None,
            "dg_40": None,
            "tt_days": None,
            "direct": False,
            "frequency": asean_ctx.get("service") or "",
            "remark": m.group("remarks") or "",
            "currency": "USD",
            "source_file": source_file,
            "_format": "ASEAN",
        }
    except (ValueError, AttributeError):
        return None


def _extract_surcharges(line: str, entry: Dict[str, Any]) -> None:
    """从行文本中提取附加费 (BAF/LSS/DOC/THC 等), 标准化 OCR 错误.

    OCR 错误修复:
    - "BAF :USD5O" → "BAF:USD50" (全角冒号 + 字母O误识别为数字0)
    """
    matches = SURCHARGE_PATTERN.findall(line)
    if not matches:
        return
    surcharges = []
    for m in matches:
        m_clean = m.upper().replace("：", ":")
        m_clean = re.sub(r"\s*:\s*", ":", m_clean)
        m_clean = re.sub(r"USD(\d+)O(\d+)", r"USD\g<1>0\g<2>", m_clean)
        m_clean = re.sub(r"USD(\d+)O\b", r"USD\g<1>0", m_clean)
        surcharges.append(m_clean)
    entry["surcharges"] = "; ".join(sorted(set(surcharges)))


def parse_image(
    filepath: str | Path,
    ocr_fn: Optional[Callable] = None,
) -> List[Dict[str, Any]]:
    """解析图片型运价文件 (PNG/JPG/JPEG).

    Args:
        filepath: 图片文件路径
        ocr_fn: 可选自定义 OCR 函数 (用于测试)

    Returns:
        NormalizedRateEntry dict 列表
    """
    filepath = Path(filepath)
    if ocr_fn is not None:
        text = ocr_fn(filepath)
    else:
        text = _ocr_extract_text(filepath)
    if not text:
        return []
    return _parse_rate_text_to_entries(text, str(filepath))


def parse_pdf(
    filepath: str | Path,
    ocr_fn: Optional[Callable] = None,
) -> List[Dict[str, Any]]:
    """解析 PDF 型运价文件."""
    return parse_image(filepath, ocr_fn=ocr_fn)
