#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D26 YMLFAKParser (2026-08-04) - YML FAK TARIFF 格式 parser.

YML FAK TARIFF 是 YML (Yang Ming Line) 国际 FAK (Freight All Kinds) 运价发布格式.
特征:
- 旧 .xls 二进制格式 (xlrd 读取)
- 4 个 sheet: Rate_Sheet / OAC / 樞紐 / OF
- OF sheet 是主运价表 (POL×POD×Equipment 矩阵)
- Equipment: DC=20' Dry, HQ=40' High Cube, RQ=Reefer

输出: NormalizedRateEntry dict 列表 (与 dg-rate-query 其他 parser 统一).

用法:
    from yml_fak_parser import parse_yml_fak
    entries = parse_yml_fak("/path/to/YML_FAK_TARIFF.xls")
    # entries 是 list of dict, 每项有 pol/pod/of_20/of_40/of_45/carrier/valid_from/valid_to
"""
from __future__ import annotations

import datetime
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import xlrd
except ImportError:
    xlrd = None


CARRIER = "YML"

# Equipment code → container type mapping
EQUIPMENT_MAP = {
    "DC": "20'GP",   # Dry Container 20'
    "HQ": "40'GP",   # High Cube 40'
    "RQ": "40'RF",   # Reefer 40'
}


def _to_date(s: Any) -> Optional[str]:
    """xlrd xldate → YYYY-MM-DD 字符串. 容错返回 None."""
    if s is None or s == "":
        return None
    try:
        d = xlrd.xldate_as_datetime(int(s), 0)  # 0 = 1900-based
        return d.strftime("%Y-%m-%d")
    except Exception:
        return None


_FILENAME_DATE_RE = re.compile(
    r"(\d{8})\s*[-_~]\s*(\d{8})"
)


def _extract_validity_from_filename(filepath: Path) -> tuple[Optional[str], Optional[str]]:
    m = _FILENAME_DATE_RE.search(filepath.name)
    if not m:
        return None, None
    try:
        dt_from = datetime.datetime.strptime(m.group(1), "%Y%m%d")
        dt_to = datetime.datetime.strptime(m.group(2), "%Y%m%d")
        return dt_from.strftime("%Y-%m-%d"), dt_to.strftime("%Y-%m-%d")
    except ValueError:
        return None, None


def _extract_validity(wb, filepath: Optional[Path] = None) -> tuple[Optional[str], Optional[str]]:
    if "Rate_Sheet" in wb.sheet_names():
        ws = wb.sheet_by_name("Rate_Sheet")
        for ri in range(min(20, ws.nrows)):
            for ci in range(ws.ncols):
                v = ws.cell_value(ri, ci)
                if isinstance(v, str) and "Issue" in v and "Date" in v:
                    val = ws.cell_value(ri, ci + 1) if ci + 1 < ws.ncols else ""
                    if val:
                        d = _to_date(val) if isinstance(val, (int, float)) else None
                        if d:
                            return d, None
    if filepath is not None:
        return _extract_validity_from_filename(filepath)
    return None, None


def _parse_of_sheet(ws: "xlrd.sheet") -> List[Dict[str, Any]]:
    """解析 OF sheet → NormalizedRateEntry 列表.

    OF 表结构 (row 6 header):
      col 0: Commodity (FAK/NOR)
      col 1: Origin (POL 5字母)
      col 2: Destination (POD 5字母)
      col 3: Shipment kind (G)
      col 4: Currency (USD)
      col 5: Equipment (DC/HQ/RQ)
      col 6-9: Receipt/Delivery/Extend/CALBASE
      col 10: 20 Amt (of_20)
      col 11: 40 Amt (of_40)
      col 12: 45 Amt (of_45)
      col 30: OF 20' (重复 10)
      col 31: OF 40' (重复 11)
      col 32-33: Add-on
      col 34-35: 20'/40' Total
    """
    entries: List[Dict[str, Any]] = []
    # row 6 (index 5) 是 header
    for ri in range(6, ws.nrows):
        commodity = str(ws.cell_value(ri, 0) or "").strip()
        if not commodity or commodity not in ("FAK", "NOR"):
            continue
        pol = str(ws.cell_value(ri, 1) or "").strip()
        pod = str(ws.cell_value(ri, 2) or "").strip()
        if not pol or not pod:
            continue
        equipment = str(ws.cell_value(ri, 5) or "").strip()
        container = EQUIPMENT_MAP.get(equipment)
        if not container:
            continue
        currency = str(ws.cell_value(ri, 4) or "").strip() or "USD"
        of_20 = ws.cell_value(ri, 10)
        of_40 = ws.cell_value(ri, 11)
        of_45 = ws.cell_value(ri, 12) if ws.ncols > 12 else None
        # 跳过全空行
        if (of_20 == "" or of_20 is None) and (of_40 == "" or of_40 is None):
            continue
        entry: Dict[str, Any] = {
            "carrier": CARRIER,
            "pol": pol,
            "pod": pod,
            "currency": currency,
            "equipment": equipment,
            "container_type": container,
        }
        if of_20 not in ("", None):
            try:
                entry["of_20"] = float(of_20)
            except (TypeError, ValueError):
                pass
        if of_40 not in ("", None):
            try:
                entry["of_40"] = float(of_40)
            except (TypeError, ValueError):
                pass
        if of_45 not in ("", None) and of_45 != "":
            try:
                entry["of_45"] = float(of_45)
            except (TypeError, ValueError):
                pass
        # 提取 add-on 费 (col 32-33, 后续可补)
        add_on_20 = ws.cell_value(ri, 32) if ws.ncols > 32 else None
        add_on_40 = ws.cell_value(ri, 33) if ws.ncols > 33 else None
        if add_on_20 not in ("", None):
            try:
                entry["add_on_20"] = float(add_on_20)
            except (TypeError, ValueError):
                pass
        if add_on_40 not in ("", None):
            try:
                entry["add_on_40"] = float(add_on_40)
            except (TypeError, ValueError):
                pass
        entries.append(entry)
    return entries


_OAC_RATE_RE = re.compile(
    r"Add-on\s+USD\s*(\d+)\s*/\s*(TEU|Box|Container)",
    re.IGNORECASE,
)

_OAC_PORT_RE = re.compile(r"T/S\s+PORTS?[:\s]\s*(.+)", re.IGNORECASE)
_OAC_SECTION_CODES = re.compile(r"\b(?!PORT\b|PORTS\b)([A-Z]{5})\b")
_HEADER_SKIP = {"LOCATION", "CODE", "ADD-ON"}


def _parse_oac_sheet(ws: "xlrd.sheet") -> List[Dict[str, Any]]:
    """解析 OAC sheet → T/S port add-on 列表.

    OAC 表结构 (多 section + 多 rate tier + 多列 port groups):
      - Row 3: "T/S PORT: CNSHA" / "T/S PORTS: CNYTN/ HKHKG" (section 头, 1-3 ports)
      - Row 4: "Add-on USD 100/TEU" (rate tier 1)
      - Row 5: "Location" | "Code" (列头标记, 2-3 列 port groups)
      - Row 6+: "南京" | "CNNKG" (左列) | "黄埔" | "CNHUA" (右列)
      - Row 21: "Add-on USD 150/TEU" (rate tier 2, 同 section 内升级)
      - Row 38: "T/S PORT: CNNGB" (新 section)
      - Row 39: "Add-on USD 100/Box" (新 unit)

    Returns:
        [{t_s_port, add_on_usd, unit, tier, category, port_code}, ...]
    """
    results: List[Dict[str, Any]] = []
    current_ports: List[str] = []
    current_add_on: Optional[int] = None
    current_unit: Optional[str] = None
    current_tier: int = 0
    current_section: str = ""
    last_rate_text: str = ""
    force_tier_increment: bool = False

    for ri in range(ws.nrows):
        row_texts = []
        for ci in range(ws.ncols):
            v = ws.cell_value(ri, ci)
            if isinstance(v, str) and v:
                row_texts.append(v.strip())
        combined_row = " ".join(row_texts)
        is_t_s_port_row = "T/S PORT" in combined_row

        for ci in range(ws.ncols):
            v = ws.cell_value(ri, ci)
            if not isinstance(v, str) or not v:
                continue
            v_strip = v.strip()
            upper = v_strip.upper()
            if v_strip.startswith("T/S PORT"):
                is_append = "T/S PORTS" in v_strip
                if is_append and is_t_s_port_row:
                    row_codes = _OAC_SECTION_CODES.findall(combined_row)
                    for code in row_codes:
                        if code not in current_ports:
                            current_ports.append(code)
                elif is_append:
                    port_match = _OAC_PORT_RE.search(v_strip)
                    if port_match:
                        payload = port_match.group(1)
                        for code in _OAC_SECTION_CODES.findall(payload):
                            if code not in current_ports:
                                current_ports.append(code)
                else:
                    port_match = _OAC_PORT_RE.search(v_strip)
                    if port_match:
                        payload = port_match.group(1)
                        port_codes = _OAC_SECTION_CODES.findall(payload)
                        if port_codes:
                            current_ports = port_codes
                current_section = v_strip
                current_tier = 0
                last_rate_text = ""
                force_tier_increment = True
                continue
            if v_strip.startswith("Add-on"):
                rate_match = _OAC_RATE_RE.search(v_strip)
                if rate_match:
                    new_rate = int(rate_match.group(1))
                    new_unit = rate_match.group(2).upper()
                    if force_tier_increment or new_rate != current_add_on or new_unit != current_unit:
                        current_add_on = new_rate
                        current_unit = new_unit
                        current_tier += 1
                    force_tier_increment = False
                    last_rate_text = v_strip
                continue
            if upper in _HEADER_SKIP:
                continue
            code_match = re.match(r"^([A-Z]{5})$", v_strip)
            if not code_match:
                continue
            if not current_ports or current_add_on is None:
                continue
            for t_s_port in current_ports:
                results.append({
                    "t_s_port": t_s_port,
                    "add_on_usd": current_add_on,
                    "unit": current_unit,
                    "tier": current_tier,
                    "section": current_section,
                    "category": last_rate_text,
                    "port_code": code_match.group(1),
                })
    return results


def _parse_hub_sheet(ws: "xlrd.sheet") -> List[Dict[str, Any]]:
    """解析 樞紐 (Hub) sheet → 中转港运价列表.

    Hub 表结构 (POL×POD 矩阵, 每 POD 2 列 20/40):
      Row 4: POD codes (BRSSZ, BRRIO, ...) - 8 个 POD
      Row 5: '加總 - 20 Amt' / '加總 - 40 Amt' 交替
      Row 6: Equipment (DC/HQ/RQ) 交替
      Row 7+: POL data rows (CNSHA, CNNGB, ...)

    Returns:
        [{pol, pod, equipment, hub_rate_20, hub_rate_40}, ...]
    """
    entries: List[Dict[str, Any]] = []
    if ws.nrows < 7 or ws.ncols < 2:
        return entries
    pod_row = 3
    pods: List[str] = []
    for ci in range(ws.ncols):
        v = ws.cell_value(pod_row, ci)
        if isinstance(v, str) and re.match(r"^[A-Z]{5}$", v.strip()):
            pods.append(v.strip())
        else:
            pods.append("")
    if not pods:
        return entries
    for ri in range(6, ws.nrows):
        pol_val = ws.cell_value(ri, 0)
        if not isinstance(pol_val, str):
            continue
        pol = pol_val.strip()
        if not pol or pol in ("Country", "DC", "HQ", "RQ", "加總 - 20 Amt", "加總 - 40 Amt"):
            continue
        if not re.match(r"^[A-Z]{2,5}$", pol) and not re.match(r"^CNSHA|^CNNGB|^CN[A-Z]+|^HKHKG|^ID[A-Z]+|^JPMP|^KR[A-Z]+|^MY[A-Z]+|^PH[A-Z]+|^SGSIN|^TH[A-Z]+|^TW[A-Z]+|^VN[A-Z]+|^BD[A-Z]+|^LK[A-Z]+|^IN[A-Z]+", pol):
            if not re.match(r"^[A-Z]{4,5}$", pol):
                continue
        for pi in range(0, min(len(pods) * 2, ws.ncols), 2):
            pod = pods[pi // 2]
            if not pod:
                continue
            rate_20 = ws.cell_value(ri, pi)
            rate_40 = ws.cell_value(ri, pi + 1) if pi + 1 < ws.ncols else None
            if rate_20 in ("", None) and rate_40 in ("", None):
                continue
            entry: Dict[str, Any] = {
                "pol": pol,
                "pod": pod,
            }
            if rate_20 not in ("", None):
                try:
                    entry["hub_rate_20"] = float(rate_20)
                except (TypeError, ValueError):
                    pass
            if rate_40 not in ("", None):
                try:
                    entry["hub_rate_40"] = float(rate_40)
                except (TypeError, ValueError):
                    pass
            if "hub_rate_20" in entry or "hub_rate_40" in entry:
                entries.append(entry)
    return entries


def parse_yml_fak(
    filepath: str | Path,
    valid_from: Optional[str] = None,
    valid_to: Optional[str] = None,
) -> List[Dict[str, Any]]:
    if xlrd is None:
        raise ImportError("需要 xlrd 库: pip install xlrd==1.2.0")
    filepath = Path(filepath)
    if not filepath.exists():
        raise FileNotFoundError(f"YML FAK 文件不存在: {filepath}")
    wb = xlrd.open_workbook(str(filepath), formatting_info=False)
    if "OF" not in wb.sheet_names():
        raise ValueError(f"YML FAK 文件缺 OF sheet: {filepath}")
    entries = _parse_of_sheet(wb.sheet_by_name("OF"))
    oac_add_ons: List[Dict[str, Any]] = []
    hub_rates: Dict[tuple, Dict[str, Any]] = {}
    if "OAC" in wb.sheet_names():
        oac_add_ons = _parse_oac_sheet(wb.sheet_by_name("OAC"))
    if "樞紐" in wb.sheet_names():
        for hr in _parse_hub_sheet(wb.sheet_by_name("樞紐")):
            hub_rates[(hr["pol"], hr["pod"])] = {
                "hub_rate_20": hr.get("hub_rate_20"),
                "hub_rate_40": hr.get("hub_rate_40"),
            }
    vf, vt = valid_from, valid_to
    if not vf or not vt:
        auto_vf, auto_vt = _extract_validity(wb, filepath)
        vf = vf or auto_vf
        vt = vt or auto_vt
    for e in entries:
        if vf:
            e["valid_from"] = vf
        if vt:
            e["valid_to"] = vt
        if oac_add_ons:
            e["oac_add_ons"] = list(oac_add_ons)
        hub = hub_rates.get((e["pol"], e["pod"]))
        if hub:
            e["hub_rate_20"] = hub.get("hub_rate_20")
            e["hub_rate_40"] = hub.get("hub_rate_40")
    return entries


def summarize(entries: List[Dict[str, Any]]) -> str:
    """返回解析结果的简短摘要 (调试用)."""
    if not entries:
        return "YMLFAK: 0 entries"
    pols = sorted({e.get("pol", "") for e in entries if e.get("pol")})
    pods = sorted({e.get("pod", "") for e in entries if e.get("pod")})
    eqs = sorted({e.get("equipment", "") for e in entries if e.get("equipment")})
    return (
        f"YMLFAK: {len(entries)} entries | "
        f"POLs: {len(pols)} ({pols[:5]}...) | "
        f"PODs: {len(pods)} ({pods[:5]}...) | "
        f"Equipment: {eqs}"
    )


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("用法: python3 yml_fak_parser.py <YML.xls>")
        sys.exit(1)
    entries = parse_yml_fak(sys.argv[1])
    print(summarize(entries))
    if len(entries) > 0:
        print("\n示例 entry:")
        for k, v in entries[0].items():
            print(f"  {k}: {v}")