# -*- coding: utf-8 -*-
"""同行汇总表/货代报价表解析器 — P0优先级
适用于：价格汇总表.xlsx、义统全航线报价、各类货代汇总报价表
特征：单表多航线分区、港口+各船司多列价格、标题按航线分区

真实结构 (义统全航线报价A, 11-sheet, 2026-08-24 样本):
  header: [carrier] POD/DESTINATION | 20'GP | 40'GP | 40'HQ | ETD | T/T | T/S | TERMINAL | EFFECTIVE | REMARK
  carrier 在 header cell[0] 或数据块首行 cell[0] (ONE/IAL/...), 后续行继承
  T/S: DIR=直航 或 中转港代码; EFFECTIVE: 9.1-9.14; 美国 sheet: POD header + T/S/TERMINAL 列序不同
  特殊行: NOR 行 (如 "新西兰 20'NOR:1727" / "NLROT/DEHAM 40'NOR: USD3748") 不产生运价条目
"""
import re
from typing import List, Tuple
from .base import BaseRateParser, register_parser, NormalizedRateEntry

_CARRIERS = ["SITC","YML","MSC","EMC","TSL","KMTC","RCL","WHL","OOCL","ONE","CMA","HMM",
             "ZIM","HPL","COSCO","SINOKOR","HEUNG-A","IAL","BENLINE","CENTRANS","CK LINE",
             "TS LINES","EVERGREEN","MAERSK","MSK","PIL","HAPAG"]

_REGION_KEYWORDS = ["东南亚","日韩","韩国","日本","中东","印巴","欧洲","地中海","红海","南美",
                    "北美","美国","澳洲","新西兰","非洲","加勒比","俄罗斯","印度"]


def _extract_sheet_region(filename: str) -> str:
    """从 filename "[sheet]" 后缀提取 sheet 名, 反查区域 (义统 sheet 即区域)."""
    m = re.search(r"\[([^\]]+)\]", filename or "")
    sheet = m.group(1) if m else ""
    for kw in _REGION_KEYWORDS:
        if kw in sheet:
            return kw
    return sheet


class ForwarderSummaryParser(BaseRateParser):
    name = "forwarder_summary"
    source_type = "forwarder_summary"

    def can_parse(self, content: str, filename: str="") -> Tuple[float, str]:
        score = 0.0
        reason = []
        if not content: return (0.0, "")
        fn = filename
        if "汇总" in fn or "报价" in fn or "全航线" in fn or "价格表" in fn:
            score += 0.3; reason.append("文件名包含汇总/报价")
        tab_count = content.count("\t")
        if tab_count > 50:
            score += 0.3; reason.append(f"制表符多({tab_count})，疑似表格")
        port_codes = len(re.findall(r"[A-Z]{5}", content))
        if port_codes > 10:
            score += 0.2; reason.append(f"多港口代码({port_codes})")
        if any(r in content for r in ["东南亚","日韩","中东","印巴","欧洲","地中海","南美","北美","澳新"]):
            score += 0.2; reason.append("含航线分区")
        carriers = sum(1 for c in _CARRIERS if c in content)
        if carriers >= 3:
            score += 0.2; reason.append(f"多船公司列({carriers}个)")
        return (min(score, 1.0), "; ".join(reason))

    def parse(self, content: str, filename: str="") -> List[NormalizedRateEntry]:
        results = []
        region = _extract_sheet_region(filename)
        lines = content.splitlines()

        # 1. 动态定位 header 行 + 列映射 (义统 11-sheet 列序不完全一致, 必须按 header 名映射)
        header_idx = None
        legacy_header_idx = None
        legacy_carrier_cols = {}
        col_map = {}
        for idx, line in enumerate(lines):
            cells = line.split("\t") if "\t" in line else line.split(",")
            joined = " | ".join(c.strip().upper() for c in cells)
            if ("20'GP" in joined or "20GP" in joined) and ("40'GP" in joined or "40GP" in joined):
                for i, c in enumerate(cells):
                    cu = c.strip().upper().replace("'", "").replace("`", "").replace('"', "")
                    if cu in ("POD", "DESTINATION", "港口", "DEST"):
                        col_map.setdefault("pod", i)
                    elif cu in ("20GP", "20'"):
                        col_map.setdefault("of_20", i)
                    elif cu in ("40GP", "40'"):
                        col_map.setdefault("of_40", i)
                    elif cu in ("40HQ", "40HC", "40'HC"):
                        col_map.setdefault("of_40hq", i)
                    elif cu in ("ETD", "班期", "船期"):
                        col_map.setdefault("etd", i)
                    elif cu in ("T/T", "TT", "航程", "中转时间"):
                        col_map.setdefault("tt", i)
                    elif cu in ("T/S", "TS", "中转", "DIR/T/S"):
                        col_map.setdefault("ts", i)
                    elif cu in ("TERMINAL", "码头", "挂靠港"):
                        col_map.setdefault("terminal", i)
                    elif cu in ("EFFECTIVE", "有效", "有效期", "VALID"):
                        col_map.setdefault("effective", i)
                    elif cu in ("REMARK", "备注", "附加费", "备注/附加费"):
                        col_map.setdefault("remark", i)
                if "pod" in col_map and "of_20" in col_map:
                    # 区分 义统模式 (pod col >= 1, carrier 在 cell[0]) vs 旧式模式
                    # (pod col == 0, carrier 在 header 各列)
                    pod_col = col_map.get("pod", -1)
                    carrier_in_header = [i for i, c in enumerate(cells)
                                         if _match_carrier(c) and i != pod_col]
                    if pod_col == 0 and carrier_in_header:
                        legacy_carrier_cols = {i: _match_carrier(cells[i]) for i in carrier_in_header}
                        legacy_header_idx = idx
                        break
                    header_idx = idx
                    break
        if legacy_header_idx is None and header_idx is None:
            for idx, line in enumerate(lines):
                cells = line.split("\t") if "\t" in line else line.split(",")
                joined = " ".join(cells).upper()
                if ("20GP" in joined or "20'" in joined or "20`" in joined or "20\"" in joined):
                    for i, c in enumerate(cells):
                        cu = c.strip().upper()
                        for carrier in _CARRIERS:
                            if carrier in cu:
                                legacy_carrier_cols[i] = carrier
                                break
                    if legacy_carrier_cols:
                        legacy_header_idx = idx
                    break

        current_carrier = ""
        last_pod = ""
        for idx, line in enumerate(lines):
            if not line.strip(): continue
            cells = line.split("\t") if "\t" in line else line.split(",")
            if len(cells) < 2: continue
            cu0 = cells[0].strip().upper()
            joined = " | ".join(c.strip().upper() for c in cells)

            if header_idx is not None:
                if idx == header_idx:
                    carrier_at_header = _match_carrier(cells[0]) if cells and cells[0].strip() else ""
                    if carrier_at_header:
                        current_carrier = carrier_at_header
                    continue
                if not any(c.strip() for c in cells[1:]) and cells[0].strip():
                    for r in ["东南亚","日韩","中东","印巴","欧洲","地中海","南美","北美","澳新","非洲","红海","黑海"]:
                        if r in cells[0]:
                            region = region or r
                            break
                    continue
                block_carrier = _match_carrier(cells[0]) if cells and cells[0].strip() else ""
                if block_carrier and block_carrier not in ("POD", "DESTINATION", "港口", "DEST"):
                    current_carrier = block_carrier
                pod_raw = cells[col_map["pod"]].strip() if col_map.get("pod", 0) < len(cells) else ""
                if "NOR" in pod_raw.upper() or "NOR：" in pod_raw:
                    continue
                if not pod_raw and last_pod:
                    pod_raw = last_pod
                if not pod_raw or len(pod_raw) < 2:
                    continue
                last_pod = pod_raw
                if not current_carrier:
                    continue
                def _cell(col):
                    c = col_map.get(col)
                    return cells[c].strip() if c is not None and c < len(cells) else ""
                p20 = self.parse_price(_cell("of_20"))
                p40 = self.parse_price(_cell("of_40"))
                p40hq = self.parse_price(_cell("of_40hq"))
                if p20 is None and p40 is None and p40hq is None:
                    continue
                e = self.new_entry(filename)
                e.pol = "CNSHA"
                e.pol_name = "上海"
                e.pod = self.normalize_port(pod_raw)
                e.pod_name = pod_raw
                e.carrier = current_carrier
                e.of_20 = p20
                e.of_40 = p40
                e.of_40hq = p40hq
                e.currency = "USD"
                e.frequency = _cell("etd")
                e.tt_days = _parse_tt(_cell("tt"))
                ts = _cell("ts").upper()
                if ts == "DIR":
                    e.direct = "Y"
                elif ts:
                    e.direct = "T" if not e.direct else e.direct
                    e.via_port = ts
                terminal = _cell("terminal")
                vf, vt = _parse_effective(_cell("effective"))
                e.valid_from = vf
                e.valid_to = vt
                remark_parts = [x for x in (_cell("remark"), terminal) if x]
                e.remark = (" | ".join(remark_parts))[:200]
                if region:
                    e.remark = (e.remark + f" | {region}")[:200] if e.remark else region
                e.raw_excerpt = line.strip()[:200]
                results.append(e)
                continue

            if legacy_header_idx is not None:
                if idx == legacy_header_idx:
                    continue
                pod_name = cells[0].strip()
                if not pod_name or len(pod_name) < 2: continue
                for col_idx, carrier in legacy_carrier_cols.items():
                    if col_idx+2 >= len(cells): continue
                    try:
                        p20 = self.parse_price(cells[col_idx] if col_idx < len(cells) else None)
                        p40 = self.parse_price(cells[col_idx+1] if col_idx+1 < len(cells) else None)
                        p40hq = self.parse_price(cells[col_idx+2] if col_idx+2 < len(cells) else None)
                        if not p20 and not p40: continue
                        e = self.new_entry(filename)
                        e.pol = "CNSHA"
                        e.pol_name = "上海"
                        e.pod = self.normalize_port(pod_name)
                        e.pod_name = pod_name
                        e.carrier = carrier
                        e.of_20 = p20
                        e.of_40 = p40
                        e.of_40hq = p40hq
                        e.currency = "USD"
                        e.raw_excerpt = line.strip()[:200]
                        results.append(e)
                    except Exception:
                        continue
        return results


def _match_carrier(cell: str) -> str:
    if not cell:
        return ""
    cu = cell.upper()
    for carrier in _CARRIERS:
        if carrier in cu:
            return carrier
    m = re.match(r"^([A-Z][A-Z &'-]{1,20})", cu)
    return m.group(1).strip() if m else ""


def _parse_tt(raw) -> int:
    if not raw:
        return None
    m = re.search(r"(\d{1,3})", str(raw).replace(",", ""))
    return int(m.group(1)) if m else None


def _parse_effective(raw) -> Tuple[str, str]:
    text = str(raw or "").strip()
    if not text or text in ("/", "-", "电询"):
        return ("", "")
    import datetime
    year = datetime.date.today().year
    m = re.match(r"^(\d{1,2})[./](\d{1,2})\s*[-~至]\s*(\d{1,2})[./](\d{1,2})$", text)
    if m:
        return (f"{year}-{int(m.group(1)):02d}-{int(m.group(2)):02d}",
                f"{year}-{int(m.group(3)):02d}-{int(m.group(4)):02d}")
    return ("", "")


register_parser(ForwarderSummaryParser())
