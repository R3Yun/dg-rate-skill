# -*- coding: utf-8 -*-
"""运价文件解析入口：自动识别格式→调用parser→输出JSON

用法:
  python parse_file.py <文件路径> [--json] [--pretty] [--sheet <sheet名>]

支持:
  - .txt / .md 纯文本（走text_parser）
  - .csv / .tsv (CargoWare模板导出的CSV)
  - .xlsx / .xls / .xlsm 二进制Excel（直接读取，无需转CSV）
  - 其他扩展名会自动读取内容后根据内容特征选择parser
"""
import argparse
import time
import json
from typing import Dict
import os
import sys
import glob

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from parsers import auto_select_parser, PARSER_REGISTRY, BaseRateParser
from rate_io import NormalizedRateEntry


def read_file(path: str, sheet_name: str = None) -> str:
    """读取文件内容，支持所有常见格式"""
    ext = os.path.splitext(path)[1].lower()

    # Excel 格式：直接读取并转为 TSV 文本
    if ext in (".xls", ".xlsx", ".xlsm"):
        try:
            from excel_helper import read_excel_to_text
            content = read_excel_to_text(path, sheet_name)
            return content
        except ImportError as e:
            print(f"[ERROR] Excel读取失败，缺少依赖: {e}", file=sys.stderr)
            print(f"  请执行: pip install openpyxl xlrd", file=sys.stderr)
            return ""
        except Exception as e:
            print(f"[ERROR] Excel读取失败: {e}", file=sys.stderr)
            return ""

    # 纯文本格式
    if ext in (".txt", ".md", ".csv", ".tsv", ".log", ""):
        for enc in ("utf-8","utf-8-sig","gb18030","gbk","big5"):
            try:
                with open(path, "r", encoding=enc) as f:
                    return f.read()
            except (UnicodeDecodeError, UnicodeError):
                continue

    # 其他尝试utf-8读
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except:
        return ""


def parse_content(content: str, filename: str=""):
    """解析文本内容"""
    parser, score, reason = auto_select_parser(content, filename)
    if not parser:
        return {"code":"error","msg":"未识别文件格式",
                "available_parsers":[p.name for p in PARSER_REGISTRY],
                "hint": "未知格式建议使用 LLM-Adaptive 模式: python inspect_excel.py + adaptive_transformer.py"}
    entries = parser.parse(content, filename)
    return {
        "code": "ok",
        "parser": parser.name,
        "confidence": score,
        "match_reason": reason,
        "entry_count": len(entries),
        "entries": [e.to_dict() for e in entries],
        "summary": build_summary(entries, filename, parser.name, score)
    }


# D59: Excel format detector metrics (跟踪 14+ 种格式调用 + entry)
_excel_metrics: Dict[str, int] = {
    "total_calls": 0,
    "cargoware_template_calls": 0,
    "sitc_notification_calls": 0,
    "forwarder_summary_calls": 0,
    "yml_fak_calls": 0,
    "tier_guide_calls": 0,
    "ial_tariff_calls": 0,
    "cul_notice_calls": 0,
    "msk_maersk_rate_calls": 0,
    "cma_cgm_rate_calls": 0,
    "evergreen_rate_calls": 0,
    "hmm_rate_calls": 0,
    "zim_rate_calls": 0,
    "yang_ming_rate_calls": 0,
    "hapag_lloyd_rate_calls": 0,
    "one_line_rate_calls": 0,
    "pil_rate_calls": 0,
    "msc_rate_calls": 0,
    "cosco_rate_calls": 0,
    "adaptive_calls": 0,
    "tier_guide_entries": 0,
    "ial_tariff_entries": 0,
    "cul_notice_entries": 0,
    "generic_rate_entries": 0,
    "adaptive_entries": 0,
}


def _get_excel_metrics() -> Dict[str, int]:
    """D59: 获取 Excel format detector metrics 快照."""
    return dict(_excel_metrics)


def _reset_excel_metrics() -> None:
    """D59: 重置 Excel format detector metrics (测试用)."""
    global _excel_metrics
    _excel_metrics = {k: 0 for k in _excel_metrics}


def parse_excel_file(path: str, sheet_name: str = None):
    """解析 Excel 文件（智能处理多 Sheet）"""
    try:
        from excel_helper import ExcelWorkbook, detect_excel_parser
    except ImportError as e:
        return {"code":"error","msg":f"缺少依赖: {e}"}

    try:
        wb = ExcelWorkbook(path)
    except Exception as e:
        return {"code":"error","msg":f"Excel读取失败: {e}"}

    all_entries = []
    all_warnings = []

    # 1. 检测应该用哪个 Parser
    parser_name, conf, reason = detect_excel_parser(path)

    # D59: 记录 metrics (调用次数 + format 分类)
    global _excel_metrics
    _excel_metrics["total_calls"] += 1
    metric_key = f"{parser_name}_calls" if f"{parser_name}_calls" in _excel_metrics else "adaptive_calls"
    _excel_metrics[metric_key] += 1

    # 2. 如果是 CargoWare 模板或货代汇总等已知格式，直接解析
    if parser_name == "cargoware_template" and conf >= 0.7:
        # CargoWare 模板只有一个数据 Sheet
        sheet = list(wb.sheets.values())[0]
        content = _sheet_to_tsv(sheet)
        result = parse_content(content, os.path.basename(path))
        if result["code"] == "ok":
            all_entries.extend([NormalizedRateEntry.from_dict(e) for e in result["entries"]])
        else:
            all_warnings.append(f"CargoWare模板解析失败: {result.get('msg','')}")

    elif parser_name == "forwarder_summary" and conf >= 0.5:
        # 货代汇总表：每个航线 Sheet 独立解析
        for sheet_name, sheet in wb.sheets.items():
            if sheet_name in ("目录","说明","备注"):
                continue
            # B1/B4 (WS-149): 费用表 sheet (订舱费/THC/附加费) 不混主表 —
            # 跳过后避免解析出非运价条目污染批次
            if any(fee_kw in sheet_name for fee_kw in ("订舱费","THC","附加费","费用表","单证费")):
                all_warnings.append(f"Sheet {sheet_name} 识别为费用表, 跳过(不混主表)")
                continue
            content = _sheet_to_tsv(sheet)
            result = parse_content(content, f"{os.path.basename(path)}[{sheet_name}]")
            if result["code"] == "ok":
                entries = [NormalizedRateEntry.from_dict(e) for e in result["entries"]]
                # B4 (WS-149): source_file 已在 filename 中带 [sheet], 不要二次拼接
                all_entries.extend(entries)
            else:
                all_warnings.append(f"Sheet {sheet_name} 解析无结果")

    elif parser_name == "tier_guide" and conf >= 0.5:
        entries = _parse_tier_guide_workbook(wb, os.path.basename(path))
        all_entries.extend(entries)
        _excel_metrics["tier_guide_entries"] += len(entries)
        if entries:
            all_warnings.append("Tier Guide 已解析为一行一POD；源文件未提供明确 Valid fm/to 时需业务员补充有效期")

    # D51: IAL 格式 (印巴/南亚航线指南)
    elif parser_name == "ial_tariff" and conf >= 0.5:
        entries = _parse_ial_tariff_workbook(wb, os.path.basename(path))
        all_entries.extend(entries)
        _excel_metrics["ial_tariff_entries"] += len(entries)
        if not entries:
            all_warnings.append("IAL Tariff 解析无结果, 需检查文件结构")

    # D51: CUL 格式 (CHINA UNITED LINES 通知)
    elif parser_name == "cul_notice" and conf >= 0.5:
        entries = _parse_cul_notice_workbook(wb, os.path.basename(path))
        all_entries.extend(entries)
        _excel_metrics["cul_notice_entries"] += len(entries)
        if not entries:
            all_warnings.append("CUL Notice 解析无结果, 需检查文件结构")

    # D52: MSK / CMA CGM / Evergreen 通用 5 价 rate
    elif parser_name in ("msk_maersk_rate", "cma_cgm_rate", "evergreen_rate") and conf >= 0.5:
        carrier_map = {"msk_maersk_rate": "MSK", "cma_cgm_rate": "CMA", "evergreen_rate": "EMC"}
        carrier = carrier_map[parser_name]
        entries = _parse_generic_rate_workbook(wb, os.path.basename(path), carrier)
        all_entries.extend(entries)
        _excel_metrics["generic_rate_entries"] += len(entries)
        if not entries:
            all_warnings.append(f"{carrier} rate 解析无结果, 需检查文件结构")

    # D53: HMM / ZIM / YANG MING / HAPAG-LLOYD / ONE 通用 5 价 rate
    elif parser_name in ("hmm_rate", "zim_rate", "yang_ming_rate", "hapag_lloyd_rate", "one_line_rate") and conf >= 0.5:
        carrier_map = {
            "hmm_rate": "HMM", "zim_rate": "ZIM", "yang_ming_rate": "YML",
            "hapag_lloyd_rate": "HPL", "one_line_rate": "ONE"
        }
        carrier = carrier_map[parser_name]
        entries = _parse_generic_rate_workbook(wb, os.path.basename(path), carrier)
        all_entries.extend(entries)
        _excel_metrics["generic_rate_entries"] += len(entries)
        if not entries:
            all_warnings.append(f"{carrier} rate 解析无结果, 需检查文件结构")

    # D54: PIL / MSC / COSCO 通用 5 价 rate
    elif parser_name in ("pil_rate", "msc_rate", "cosco_rate") and conf >= 0.5:
        carrier_map = {"pil_rate": "PIL", "msc_rate": "MSC", "cosco_rate": "COS"}
        carrier = carrier_map[parser_name]
        entries = _parse_generic_rate_workbook(wb, os.path.basename(path), carrier)
        all_entries.extend(entries)
        _excel_metrics["generic_rate_entries"] += len(entries)
        if not entries:
            all_warnings.append(f"{carrier} rate 解析无结果, 需检查文件结构")

    else:
        # 未知格式：尝试所有 Sheet 解析，或提示用 LLM-Adaptive
        for sheet_name, sheet in wb.sheets.items():
            content = _sheet_to_tsv(sheet)
            result = parse_content(content, f"{os.path.basename(path)}[{sheet_name}]")
            if result["code"] == "ok" and result["entry_count"] > 0:
                entries = [NormalizedRateEntry.from_dict(e) for e in result["entries"]]
                for e in entries:
                    e.source_file = f"{e.source_file}[{sheet_name}]"
                all_entries.extend(entries)
                _excel_metrics["adaptive_entries"] += len(entries)  # D59: 计数

    # 3. 如果常规 Parser 解析到的条目太少，建议用 LLM-Adaptive
    if len(all_entries) < 3:
        all_warnings.append("解析条目过少，建议试用 LLM-Adaptive 模式: python inspect_excel.py xxx.xlsx")

    return {
        "code": "ok",
        "parser": "excel_auto",
        "detected_format": parser_name,
        "confidence": conf,
        "match_reason": reason,
        "sheet_count": len(wb.sheets),
        "sheets_processed": list(wb.sheets.keys()),
        "entry_count": len(all_entries),
        "entries": [e.to_dict() for e in all_entries],
        "warnings": all_warnings,
        "summary": build_summary(all_entries, os.path.basename(path), "excel_auto", conf),
        # D59: 加入 Excel format detector metrics (对称 D58 ocr-image.py)
        "_excel_metrics": _get_excel_metrics(),
    }


def _sheet_to_tsv(sheet) -> str:
    """将工作表转为 TSV 格式文本"""
    lines = []
    for row in sheet.rows:
        clean_row = [str(c).replace("\t", " ").replace("\n", " ").strip() for c in row]
        lines.append("\t".join(clean_row))
    return "\n".join(lines)


def _parse_tier_guide_workbook(wb, filename: str):
    """Parse TS Lines/Tier Guide horizontal multi-POD workbook.

    Typical structure:
    SVC | VESSEL | VOYAGE | ... | ETD | ATD | POD | 20GP | 40GP | 40HQ | POD | ...
    The source normally has sailing ETD but no explicit rate validity, so valid_from/to
    intentionally stay blank and must be confirmed by the user before import.
    """
    entries = []
    for sheet_name, sheet in wb.sheets.items():
        header_idx = None
        pod_cols = []
        for idx, row in enumerate(sheet.rows):
            normalized = [str(c).strip().upper() for c in row]
            candidates = []
            for col, cell in enumerate(normalized):
                if cell == "POD" and col + 3 < len(normalized):
                    if normalized[col + 1] == "20GP" and normalized[col + 2] == "40GP" and normalized[col + 3] == "40HQ":
                        candidates.append(col)
            if candidates:
                header_idx = idx
                pod_cols = candidates
                break
        if header_idx is None or not pod_cols:
            continue

        for row in sheet.rows[header_idx + 1:]:
            if not any(str(c).strip() for c in row):
                continue
            svc = _cell(row, 0)
            vessel = _cell(row, 1)
            voyage = _cell(row, 2)
            etd = _fmt_date(_cell(row, 4))
            if not svc and not vessel and not voyage:
                continue
            for pod_col in pod_cols:
                pod_name = _cell(row, pod_col)
                if not pod_name or pod_name.upper() in {"POD", "OMIT", "NIL", "-", "/"}:
                    continue
                of_20 = _parse_number(_cell(row, pod_col + 1))
                of_40 = _parse_number(_cell(row, pod_col + 2))
                of_40hq = _parse_number(_cell(row, pod_col + 3))
                if of_20 is None and of_40 is None and of_40hq is None:
                    continue
                entry = NormalizedRateEntry()
                entry.pol = "CNSHA"
                entry.pol_name = "上海"
                entry.pod_name = pod_name
                entry.pod = _normalize_port_safe(pod_name)
                entry.pc = "Both"
                entry.carrier = "TS Lines"
                entry.carrier_name = "德翔海运"
                entry.rate_type = "FCL3.1"
                entry.container_type = "GP"
                entry.vessel = vessel
                entry.voyage = voyage
                entry.etd = etd
                entry.of_20 = of_20
                entry.of_40 = of_40
                entry.of_40hq = of_40hq
                entry.currency = "USD"
                entry.remark = "SVC: " + svc if svc else ""
                entry.source_file = f"{filename}[{sheet_name}]"
                entry.source_type = "excel_tier_guide"
                entry.parser = "tier_guide"
                entry.confidence = 0.9
                entry.raw_excerpt = " | ".join([x for x in [svc, vessel, voyage, etd, pod_name] if x])
                entries.append(entry)
    return entries


def _parse_ial_tariff_workbook(wb, filename: str):
    """D51: 解析 IAL Guideline Rate 格式 (印度/巴基斯坦/斯里兰卡航线).

    典型结构 (Tariff sheet):
    row 5: 'India/Pakistan/Sri Lanka' (Region)
    row 6: 'Region' | 'POD' | 'CODE' | 'SVC' | 'SHA ETD' | 'Ocean Freight(USD)' | ...
    row 7+: data rows (Region, POD, CODE, SVC, SHA ETD, 2 prices)
    """
    entries = []
    for sheet_name, sheet in wb.sheets.items():
        if sheet_name.lower() not in ("tariff", "rate", "ial"):
            continue
        header_idx = None
        for idx, row in enumerate(sheet.rows):
            normalized = [str(c).strip().upper() for c in row]
            if "POD" in normalized and "SVC" in normalized and ("OCEAN FREIGHT" in " ".join(normalized) or "FREIGHT" in " ".join(normalized)):
                header_idx = idx
                break
        if header_idx is None:
            continue

        col_map = {}
        for col, cell in enumerate([str(c).strip().upper() for c in sheet.rows[header_idx]]):
            if cell in ("REGION",):
                col_map["region"] = col
            elif cell in ("POD", "PORT"):
                col_map["pod"] = col
            elif cell in ("CODE", "UN/LOCODE", "UNLOCODE"):
                col_map["code"] = col
            elif cell in ("SVC", "SERVICE"):
                col_map["svc"] = col
            elif cell in ("SHA ETD", "ETD"):
                col_map["etd"] = col
            elif cell in ("OCEAN FREIGHT(USD)", "OCEAN FREIGHT", "20GP", "20'"):
                col_map["of_20"] = col
            elif cell in ("40GP", "40'", "40HC"):
                col_map["of_40"] = col
            elif cell in ("DIR OR T/S", "DIR", "TRANSIT"):
                col_map["transit"] = col

        # D51 修复: IAL 文件 col 6 常为空 header (40' 价格藏在 col 6)
        # 如果 of_20 在 col 5, 假设 of_40 在 col 6 (除非有明确 40GP/40HC header)
        if "of_20" in col_map and "of_40" not in col_map and col_map["of_20"] + 1 < len(sheet.rows[header_idx]):
            next_col = col_map["of_20"] + 1
            next_cell = str(sheet.rows[header_idx][next_col]).strip() if next_col < len(sheet.rows[header_idx]) else ""
            if not next_cell or next_cell.upper() in ("NONE", "NULL", ""):
                col_map["of_40"] = next_col

        for row in sheet.rows[header_idx + 1:]:
            if not any(str(c).strip() for c in row):
                continue
            pod_name = _cell(row, col_map.get("pod", 1)) if "pod" in col_map else ""
            if not pod_name:
                continue
            of_20 = _parse_number(_cell(row, col_map["of_20"])) if "of_20" in col_map else None
            of_40 = _parse_number(_cell(row, col_map["of_40"])) if "of_40" in col_map else None
            if of_20 is None and of_40 is None:
                continue
            entry = NormalizedRateEntry()
            entry.pol = "CNSHA"
            entry.pol_name = "上海"
            entry.pod = pod_name
            entry.pod_name = pod_name
            entry.carrier = "IAL"
            entry.service = _cell(row, col_map.get("svc", 3)) if "svc" in col_map else ""
            entry.voyage = ""
            entry.etd = _fmt_date(_cell(row, col_map.get("etd", 4))) if "etd" in col_map else ""
            entry.of_20 = of_20
            entry.of_40 = of_40
            entry.of_40hq = None
            entry.dg_20 = None
            entry.dg_40 = None
            entry.currency = "USD"
            entry.remark = _cell(row, col_map.get("transit", 7)) if "transit" in col_map else ""
            entry.source_file = f"{filename}[{sheet_name}]"
            entry.source_type = "excel_ial_tariff"
            entry.parser = "ial_tariff"
            entry.confidence = 0.85
            entry.raw_excerpt = " | ".join([x for x in [_cell(row, col_map.get("svc", 3)) if "svc" in col_map else "", _cell(row, col_map.get("etd", 4)) if "etd" in col_map else "", pod_name] if x])
            entries.append(entry)
    return entries


def _parse_cul_notice_workbook(wb, filename: str):
    """D51: 解析 CUL Notice 格式 (CHINA UNITED LINES 船期通知).

    典型结构 (工作表1):
    row 1: 'CHINA UNITED LINES LTD. YYYY/MM/DD' (标题)
    row 2: 航线 | 船名 | 港口 | 航程 | Selling Rate | ... | REMARK
    row 3: (sub-header) ETD | 20GP | 40HC
    row 4+: data (航线/船名/港口/ETD/2 prices), 续行继承船名航次
    """
    entries = []
    for sheet_name, sheet in wb.sheets.items():
        if not sheet.rows:
            continue
        first_text = " ".join(str(c).strip() for c in sheet.rows[0] if c)
        if "CHINA UNITED LINES" not in first_text.upper() and "CUL" not in first_text.upper()[:20]:
            continue

        header_idx = None
        for idx, row in enumerate(sheet.rows[:5]):
            normalized = [str(c).strip() for c in row]
            if "Selling Rate" in normalized or ("航线" in normalized and "船名" in normalized):
                header_idx = idx
                break
        if header_idx is None:
            continue

        col_map = {}
        for col, cell in enumerate([str(c).strip() for c in sheet.rows[header_idx]]):
            if cell == "航线":
                col_map["line"] = col
            elif cell == "船名":
                col_map["vessel"] = col
            elif cell == "港口":
                col_map["pod"] = col
            elif cell in ("航程", "ETD"):
                col_map["etd"] = col
            elif cell in ("Selling Rate", "20GP", "20'"):
                col_map["of_20"] = col
            elif cell in ("40HC", "40HQ", "40'"):
                col_map["of_40"] = col

        last_vessel = ""
        for row in sheet.rows[header_idx + 1:]:
            if not any(str(c).strip() for c in row):
                continue
            vessel_cell = _cell(row, col_map.get("vessel", 1)) if "vessel" in col_map else ""
            if vessel_cell:
                last_vessel = vessel_cell
            pod_name = _cell(row, col_map.get("pod", 2)) if "pod" in col_map else ""
            if not pod_name:
                continue
            of_20 = _parse_number(_cell(row, col_map["of_20"])) if "of_20" in col_map else None
            of_40 = _parse_number(_cell(row, col_map["of_40"])) if "of_40" in col_map else None
            if of_20 is None and of_40 is None:
                continue
            entry = NormalizedRateEntry()
            entry.pol = "CNSHA"
            entry.pol_name = "上海"
            entry.pod = pod_name
            entry.pod_name = pod_name
            entry.carrier = "CUL"
            entry.service = _cell(row, col_map.get("line", 0)) if "line" in col_map else ""
            entry.vessel = last_vessel
            entry.voyage = last_vessel.split()[-1] if last_vessel and last_vessel.split()[-1][-1].isdigit() else ""
            entry.etd = _fmt_date(_cell(row, col_map.get("etd", 3))) if "etd" in col_map else ""
            entry.of_20 = of_20
            entry.of_40 = of_40
            entry.of_40hq = None
            entry.dg_20 = None
            entry.dg_40 = None
            entry.currency = "USD"
            entry.source_file = f"{filename}[{sheet_name}]"
            entry.source_type = "excel_cul_notice"
            entry.parser = "cul_notice"
            entry.confidence = 0.85
            entry.raw_excerpt = " | ".join([x for x in [last_vessel, pod_name, entry.etd] if x])
            entries.append(entry)
    return entries


def _parse_generic_rate_workbook(wb, filename: str, carrier: str):
    """D52: 通用 5 价 rate workbook parser (MSK/EMC/CMA CGM 等).

    典型结构 (sheet 1):
    row 1: 'POL' | 'POD' | 'VIA中转港' | '船公司' | '订舱代理' | '20GP O/F(USD)' | '40GP O/F(USD)' | '40HQ O/F(USD)' | '20NOR O/F(USD)' | '40NOR O/F(USD)' | 'ETD' | ...
    row 2+: 数据行 (POL/POD/VIA/船公司/订舱代理/5 prices)

    MSK JSON 示例 (scratch/MSK-RU.json):
    fields: ['POL', 'POD', 'VIA中转港', '船公司', '订舱代理', '20GP O/F(USD)', '40GP O/F(USD)', '40HQ O/F(USD)', '20NOR O/F(USD)', '40NOR O/F(USD)']
    row 0: ['CNSHA', 'RUVVO', '', 'MSK', '', 1200, 2000, 2050, None, None]
    """
    entries = []
    for sheet_name, sheet in wb.sheets.items():
        if not sheet.rows:
            continue
        header_idx = None
        for idx, row in enumerate(sheet.rows[:5]):
            normalized = [str(c).strip() for c in row]
            if "POL" in normalized and "POD" in normalized:
                header_idx = idx
                break
        if header_idx is None:
            continue

        col_map = {}
        for col, cell in enumerate([str(c).strip().upper() for c in sheet.rows[header_idx]]):
            if cell == "POL":
                col_map["pol"] = col
            elif cell == "POD":
                col_map["pod"] = col
            elif "VIA" in cell:
                col_map["via"] = col
            elif cell in ("船公司", "CARRIER"):
                col_map["carrier"] = col
            elif cell in ("订舱代理", "BOOKING AGENT"):
                col_map["agent"] = col
            elif cell in ("20GP O/F(USD)", "20GP", "20'"):
                col_map["of_20"] = col
            elif cell in ("40GP O/F(USD)", "40GP", "40'"):
                col_map["of_40"] = col
            elif cell in ("40HQ O/F(USD)", "40HQ", "40'HC"):
                col_map["of_40hq"] = col
            elif cell in ("20NOR O/F(USD)", "20NOR"):
                col_map["of_20nor"] = col
            elif cell in ("40NOR O/F(USD)", "40NOR"):
                col_map["of_40nor"] = col
            elif cell in ("ETD", "SAILING"):
                col_map["etd"] = col

        for row in sheet.rows[header_idx + 1:]:
            if not any(str(c).strip() for c in row):
                continue
            pod_name = _cell(row, col_map.get("pod", 1)) if "pod" in col_map else ""
            if not pod_name:
                continue
            entry = NormalizedRateEntry()
            entry.pol = _cell(row, col_map.get("pol", 0)) if "pol" in col_map else ""
            entry.pol_name = entry.pol
            entry.pod = pod_name
            entry.pod_name = pod_name
            entry.carrier = carrier
            entry.service = _cell(row, col_map.get("carrier", 3)) if "carrier" in col_map else carrier
            entry.vessel = ""
            entry.voyage = ""
            entry.etd = _fmt_date(_cell(row, col_map.get("etd", 10))) if "etd" in col_map else ""
            entry.of_20 = _parse_number(_cell(row, col_map["of_20"])) if "of_20" in col_map else None
            entry.of_40 = _parse_number(_cell(row, col_map["of_40"])) if "of_40" in col_map else None
            entry.of_40hq = _parse_number(_cell(row, col_map["of_40hq"])) if "of_40hq" in col_map else None
            entry.of_20nor = _parse_number(_cell(row, col_map["of_20nor"])) if "of_20nor" in col_map else None
            entry.of_40nor = _parse_number(_cell(row, col_map["of_40nor"])) if "of_40nor" in col_map else None
            entry.dg_20 = None
            entry.dg_40 = None
            entry.currency = "USD"
            entry.remark = _cell(row, col_map.get("via", 2)) if "via" in col_map else ""
            entry.via_port = entry.remark
            entry.source_file = f"{filename}[{sheet_name}]"
            entry.source_type = f"excel_{carrier.lower()}_rate"
            entry.parser = carrier.lower()
            entry.confidence = 0.8
            entry.raw_excerpt = " | ".join([x for x in [entry.pol, pod_name, entry.service] if x])
            entries.append(entry)
    return entries


def _cell(row, idx: int) -> str:
    if idx >= len(row):
        return ""
    value = row[idx]
    return "" if value is None else str(value).strip()


def _parse_number(value: str):
    text = str(value or "").strip().replace(",", "")
    if not text or text.upper() in {"/", "-", "OMIT", "NIL"}:
        return None
    try:
        number = float(text)
        return int(number) if number == int(number) else number
    except Exception:
        return None


def _fmt_date(value: str) -> str:
    text = str(value or "").strip()
    if not text or text.upper() in {"OMIT", "NIL", "/", "-"}:
        return ""
    import re
    m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", text)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return text


def _normalize_port_safe(pod_name: str) -> str:
    text = str(pod_name or "").strip()
    code = BaseRateParser.normalize_port(text)
    if code and code != text:
        return code
    if text.upper().endswith("S") and len(text) > 4:
        singular = text[:-1]
        code2 = BaseRateParser.normalize_port(singular)
        if code2 and code2 != singular:
            return code2
    return code or text


def build_summary(entries, filename, parser, score):
    """生成解析摘要"""
    if not entries:
        return "未解析到任何运价记录"
    pols = set(e.pol for e in entries if e.pol)
    pods = set(e.pod for e in entries if e.pod)
    carriers = set(e.carrier for e in entries if e.carrier)
    dg_count = sum(1 for e in entries if e.dg_surcharges)
    v_from = min((e.valid_from for e in entries if e.valid_from), default="")
    v_to = max((e.valid_to for e in entries if e.valid_to), default="")
    return (f"文件 {os.path.basename(filename)} 由 {parser} 解析(置信度{int(score*100)}%)，"
            f"共 {len(entries)} 条运价记录，"
            f"涉及 {len(pols)}个起运港/{len(pods)}个目的港/{len(carriers)}个船司，"
            f"其中 {dg_count} 条含DG附加费，"
            f"有效期 {v_from} ~ {v_to}")



def maybe_ocr(path):
    """如果 path 是图片或 PDF，先 OCR 转 Markdown，再返回 MD 临时文件路径。
    否则原样返回路径。"""
    try:
        from ocr_adapter import needs_ocr, run_ocr
    except Exception as e:
        sys.stderr.write("[WARN] ocr_adapter 不可用，直接读取原文件: " + str(e) + "\n")
        return path
    if not needs_ocr(path):
        return path
    sys.stderr.write("[INFO] 检测到图像/PDF，先走 OCR (mineru-open-api)...\n")
    res = run_ocr(path)
    if res.get("code") != "ok":
        sys.stderr.write("[ERROR] OCR 失败: " + str(res) + "\n")
        return path
    sys.stderr.write("[INFO] OCR 完成: " + res["source"] + " (" + str(res["md_chars"]) + " chars)\n")
    return res["cache"]
def main():
    ap = argparse.ArgumentParser(description="DG运价文件解析器（支持Excel直接读取）")
    ap.add_argument("path", help="文件路径或glob模式(如\"*.xlsx\")")
    ap.add_argument("--pretty", action="store_true", help="美化JSON输出")
    ap.add_argument("--list-parsers", action="store_true", help="列出所有可用parser")
    ap.add_argument("--sheet", help="指定Excel工作表名称(多Sheet时)")
    ap.add_argument("--inspect", action="store_true", help="仅检查Excel结构(供LLM-Adaptive)")
    args = ap.parse_args()

    if args.list_parsers:
        for p in PARSER_REGISTRY:
            print(f"  {p.name:30s} [{p.source_type}]")
        return

    # 仅检查 Excel 结构
    if args.inspect:
        try:
            from excel_helper import excel_to_raw_structure
            struct = excel_to_raw_structure(args.path)
            print(json.dumps(struct, ensure_ascii=False, indent=2 if args.pretty else None))
        except Exception as e:
            print(json.dumps({"code":"error","msg":str(e)}, ensure_ascii=False))
        return

    files = []
    if any(c in args.path for c in "*?[]"):
        files = glob.glob(args.path)
    elif os.path.isdir(args.path):
        for ext in ("*.txt","*.csv","*.tsv","*.xlsx","*.xls"):
            files.extend(glob.glob(os.path.join(args.path, ext)))
    else:
        files = [args.path]

    all_results = []
    # 解析缓存：同文件 hash 命中时不重解析
    cache = None
    try:
        from parse_cache import ParseCache
        cache = ParseCache()
    except Exception as e:
        sys.stderr.write("[WARN] parse_cache 不可用: " + str(e) + "\n")

    for fp in files:
        if not os.path.isfile(fp):
            print(f"[WARN] 文件不存在: {fp}", file=sys.stderr)
            continue

        # 1. 缓存命中检查
        if cache:
            hit = cache.get(fp)
            if hit and "parse_result" in hit:
                sys.stderr.write("[CACHE HIT] " + fp + " -> " + hit.get("_cache", {}).get("key", "")[:8] + "\n")
                result = hit["parse_result"]
                if isinstance(result, list):
                    all_results.extend(result)
                else:
                    all_results.append(result)
                continue

        # 2. 缓存未命中 → 走 OCR + 解析
        fp = maybe_ocr(fp)
        ext = os.path.splitext(fp)[1].lower()
        if ext in (".xls", ".xlsx", ".xlsm"):
            # Excel 文件用专用逻辑解析
            result = parse_excel_file(fp, args.sheet)
            result["file"] = fp
        else:
            # 普通文本文件
            content = read_file(fp, args.sheet)
            if not content:
                continue
            result = parse_content(content, os.path.basename(fp))
            result["file"] = fp
        all_results.append(result)

        # 3. 写缓存
        if cache and result.get("code") == "ok":
            try:
                cache.set(fp, {
                    "parse_result": result,
                    "source_path": fp,
                    "parsed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                })
            except Exception as e:
                sys.stderr.write("[WARN] 写缓存失败: " + str(e) + "\n")

    output = json.dumps(all_results if len(all_results)>1 else (all_results[0] if all_results else {"code":"error","msg":"无文件"}),
                        ensure_ascii=False, indent=2 if args.pretty else None)
    print(output)


if __name__ == "__main__":
    main()

