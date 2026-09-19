# -*- coding: utf-8 -*-
"""Markdown pipe table 解析器 — 主要给 OCR 输出用
支持 | col | col | ... | 形式，至少 2 行 + 1 分隔行（| --- | --- |）。
识别表头包含 POL/POD/20GP/40GP/DG 等关键词。

2026-07-10 增强：
  - 每行 entry 加 confidence（基于：表头完整度 + 数字字段完整性 + 异常词检查）
  - 加 异常词 扫描（OCR 常见误识词）
  - 加 短乱码 扫描（连续 3+ 个不可识别字符）
  - 命中异常词的字段值会被原样保留但在 e.warnings 中标注
"""
import re
import datetime
from typing import List, Tuple
from .base import BaseRateParser, register_parser, NormalizedRateEntry, DGSurcharge


COL_ALIASES = {
    "POL": ["POL", "起运港", "装货港", "LOAD"],
    "POD": ["POD", "目的港", "卸货港", "DISCH"],
    "20GP": ["20GP", "20'", "20GP O/F", "20GP OF"],
    "40GP": ["40GP", "40'", "40GP O/F"],
    "40HQ": ["40HQ", "40HQ O/F"],
    "40NOR": ["40NOR"],
    "DG": ["DG", "DG Surcharge", "DG附加费", "DG加价", "附加费"],
    "ETA": ["ETA", "航程", "TT", "TRANSIT"],
    "VIA": ["VIA", "中转", "T/S"],
    "CARRIER": ["CARRIER", "船公司"],
    "POL_NAME": ["POL NAME", "起运港名"],
    "POD_NAME": ["POD NAME", "目的港名"],
}


# 已知 OCR 误识词（业务专家标注，持续扩充）
# 出现在结构化字段（pol/pod/carrier 等）时即告警；不出现在 备注 中（备注是自由文本）
OCR_GARBAGE_WORDS = [
    # === 中文 OCR 误识 ===
    "备清高中", "件上", "件上下", "件下",
    "上包", "高包", "低包", "中包",
    "发航", "始发", "始发港",
    "付运", "接运", "水水", "中中", "转转",
    "上订", "下订", "补订",
    "上上", "下下", "上中", "下中", "中高", "高下",
    "海海", "丰丰", "亚亚",
    "转上", "转下", "转中",
    "上代", "下代",
    "运代", "船公", "公船", "运公", "公运",
    "公司司", "司司", "公司公", "公公",
    "上港", "下港", "中港", "高港",
    "印尼", "度尼", "度尼西亚", "尼西亚",
    "BANGKOKKK", "SINGAPOREEE", "BKKKK", "SINNNN",
    "POLLLL", "PODLLL", "SHIPPPP",
    "曼谷谷", "新加坡坡", "香港港", "上海海",
    "BANGKOKK", "SINGAPOREE", "BANGKOOK", "SINGAPOO",
]

# 短乱码检测：3+ 连续不可读字符
GARBAGE_RUN_RE = re.compile(r"[^\u4e00-\u9fa5A-Za-z0-9\s\.\,\-\(\)\/\\\:\;\+\*\#\$\%\&]{3,}")


def _has_garbage_word(s: str):
    """扫描字符串，返回所有命中的异常词。"""
    if not s:
        return []
    hits = []
    upper = s.upper()
    for w in OCR_GARBAGE_WORDS:
        if w.upper() in upper:
            hits.append(w)
    for m in GARBAGE_RUN_RE.findall(s):
        hits.append("乱码片段:" + repr(m))
    return hits


class MarkdownTableParser(BaseRateParser):
    name = "markdown_table"
    source_type = "ocr_markdown"

    def can_parse(self, content: str, filename: str = "") -> Tuple[float, str]:
        if not content:
            return (0.0, "")
        lines = [l.rstrip() for l in content.splitlines() if l.strip()]
        if len(lines) < 3:
            return (0.0, "")
        sep_idx = -1
        for i, ln in enumerate(lines):
            if re.match(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$", ln):
                sep_idx = i
                break
        if sep_idx < 0 or sep_idx >= len(lines) - 1:
            return (0.0, "no_separator|lines=" + str(len(lines)))
        header = None
        for cand in [sep_idx - 1, sep_idx + 1, sep_idx - 2, sep_idx + 2]:
            if 0 <= cand < len(lines):
                cand_hdr = self._split_row(lines[cand])
                cand_lower = [c.lower() for c in cand_hdr]
                if any(any(a.lower() in c for a in COL_ALIASES['POL']) for c in cand_lower):
                    header = cand_hdr
                    break
        if not header:
            return (0.0, 'no header with POL after separator')
        h_lower = [c.lower() for c in header]
        pol_hit = any(any(a.lower() in c.lower() for a in COL_ALIASES["POL"]) for c in h_lower)
        pod_hit = any(any(a.lower() in c.lower() for a in COL_ALIASES["POD"]) for c in h_lower)
        if not (pol_hit and pod_hit):
            return (0.0, "")
        score = 0.7
        body_rows = lines[sep_idx + 1:]
        numeric_rows = sum(1 for r in body_rows[:10] if re.search(r"\d{3,}", r))
        if numeric_rows >= 3:
            score += 0.3
        reason = "markdown pipe table 含 POL/POD 表头 + 多数值行"
        return (min(score, 1.0), reason)

    def parse(self, content: str, filename: str = "") -> List[NormalizedRateEntry]:
        results: List[NormalizedRateEntry] = []
        lines = [l.rstrip() for l in content.splitlines() if l.strip()]
        sep_idx = -1
        for i, ln in enumerate(lines):
            if re.match(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$", ln):
                sep_idx = i
                break
        if sep_idx <= 0:
            return results
        header = None
        for cand in [sep_idx - 1, sep_idx + 1, sep_idx - 2, sep_idx + 2]:
            if 0 <= cand < len(lines):
                cand_hdr = self._split_row(lines[cand])
                cand_lower = [c.lower() for c in cand_hdr]
                if any(any(a.lower() in c for a in COL_ALIASES['POL']) for c in cand_lower):
                    header = cand_hdr
                    break
        if not header:
            return results
        col_map = self._map_columns(header)
        if not col_map or ("POL" not in col_map and "POD" not in col_map):
            return results

        # 全局上下文（文件头若干行找 carrier / 日期）
        carrier, currency = "", "USD"
        valid_from, valid_to = "", ""
        for ln in lines[:sep_idx] + [lines[sep_idx]]:
            for name in ["SITC", "TSL", "YML", "MSC", "MSK", "EMC", "CMA", "ONE",
                         "HMM", "ZIM", "OOCL", "KMTC", "RCL", "IAL", "WHL",
                         "兴亚", "海丰", "德翔", "阳明", "地中海", "马士基",
                         "长荣", "达飞", "中远", "COSCO", "PIL", "WANHAI",
                         "SNL", "SINOKOR", "HEUNG-A"]:
                if name in ln and not carrier:
                    carrier = self._map_carrier(name)
            vf, vt = self.parse_date(ln)
            if vf and not valid_from:
                valid_from = vf
            if vt and not valid_to:
                valid_to = vt
            if "RMB" in ln or "人民币" in ln:
                currency = "RMB"

        # 数据行：跳过 header 行
        if sep_idx + 1 < len(lines) and header == self._split_row(lines[sep_idx + 1]):
            body = lines[sep_idx + 2:]
        else:
            body = lines[sep_idx + 1:]
        for ln in body:
            row = self._split_row(ln)
            if not row or all(not c.strip() for c in row):
                continue
            if not any(re.match(r"^[A-Z]{5}", c) or self._looks_chinese(c) for c in row):
                continue
            e = self.new_entry(filename)
            for k, idx in col_map.items():
                if idx >= len(row):
                    continue
                cell = row[idx].strip()
                if k == "POL":
                    e.pol = self.normalize_port(cell)
                    if not re.match(r"^[A-Z]{5}", e.pol):
                        e.pol = cell.upper()
                    e.pol_name = cell
                elif k == "POD":
                    e.pod = self.normalize_port(cell)
                    if not re.match(r"^[A-Z]{5}", e.pod):
                        e.pod = cell.upper()
                    e.pod_name = cell
                elif k == "20GP":
                    e.of_20 = self.parse_price(cell)
                elif k == "40GP":
                    e.of_40 = self.parse_price(cell)
                elif k == "40HQ":
                    e.of_40hq = self.parse_price(cell)
                elif k == "40NOR":
                    e.of_40nor = self.parse_price(cell)
                elif k == "DG":
                    dg = self._parse_dg_cell(cell)
                    if dg:
                        e.dg_surcharges.append(dg)
                elif k == "ETA":
                    e.tt_days = self._to_int(cell)
                elif k == "VIA":
                    e.via_port = cell
                elif k == "POL_NAME":
                    e.pol_name = cell
                elif k == "POD_NAME":
                    e.pod_name = cell
            if not e.pol and not e.pod:
                continue
            if not e.carrier:
                e.carrier = carrier
            if not e.valid_from:
                e.valid_from = valid_from
            if not e.valid_to:
                e.valid_to = valid_to
            e.currency = currency or e.currency
            # 异常词 / 乱码 扫描
            self._audit_garbage(e)
            # 置信度
            e.confidence = self._calc_confidence(e, header, col_map)
            results.append(e)
        return results

    # ---------- 异常词 / 置信度 ----------
    def _audit_garbage(self, e: NormalizedRateEntry):
        """扫描 entry 的关键字段，命中异常词加入 warnings。"""
        targets = [
            ("carrier", e.carrier),
            ("pol_name", e.pol_name),
            ("pod_name", e.pod_name),
            ("via_port", e.via_port),
            ("booking_agent", e.booking_agent),
            ("frequency", e.frequency),
            ("direct", e.direct),
        ]
        for fname, val in targets:
            hits = _has_garbage_word(val or "")
            for h in hits:
                e.warnings.append(fname + " 命中 OCR 异常词: " + h + " (原文=" + repr(val) + ")")

    def _calc_confidence(self, e: NormalizedRateEntry,
                         header: List[str], col_map: dict) -> float:
        score = 0.5
        if re.match(r"^[A-Z]{5}$", e.pol or ""):
            score += 0.1
        if re.match(r"^[A-Z]{5}$", e.pod or ""):
            score += 0.1
        if e.of_20 or e.of_40 or e.of_40hq:
            score += 0.1
        if e.valid_from and e.valid_to:
            score += 0.1
        if e.carrier:
            score += 0.05
        if e.warnings:
            score -= 0.05 * len(e.warnings)
        return max(0.0, min(1.0, round(score, 3)))

    # ---------- helpers ----------
    def _split_row(self, ln: str) -> List[str]:
        s = ln.strip()
        if s.startswith("|"): s = s[1:]
        if s.endswith("|"): s = s[:-1]
        if not s: return []
        return [c.strip() for c in s.split("|")]

    def _map_columns(self, header: List[str]) -> dict:
        m = {}
        for i, h in enumerate(header):
            hu = h.upper()
            for key, aliases in COL_ALIASES.items():
                if any(a.upper() in hu for a in aliases) and key not in m:
                    m[key] = i
                    break
        return m

    def _looks_chinese(self, s: str) -> bool:
        return bool(s) and bool(re.search(r"[\u4e00-\u9fa5]", s))

    def _to_int(self, s: str):
        m = re.search(r"\d+", s or "")
        return int(m.group()) if m else None

    def _map_carrier(self, name: str) -> str:
        table = {
            "SITC": "SITC", "海丰": "SITC", "TSL": "TSL", "YML": "YML",
            "阳明": "YML", "MSC": "MSC", "地中海": "MSC", "MSK": "MSK",
            "马士基": "MSK", "EMC": "EMC", "长荣": "EMC", "CMA": "CMA",
            "达飞": "CMA", "ONE": "ONE", "HMM": "HMM", "ZIM": "ZIM",
            "OOCL": "OOCL", "东方海外": "OOCL", "KMTC": "KMTC",
            "兴亚": "HEUNG-A", "HEUNG-A": "HEUNG-A", "HEUNGA": "HEUNG-A",
            "德翔": "WHL", "WHL": "WHL", "RCL": "RCL", "IAL": "IAL",
            "SNL": "SNL", "中外运": "SNL", "SINOKOR": "SINOKOR",
            "COSCO": "COSCO", "中远": "COSCO", "PIL": "PIL",
            "WANHAI": "WANHAI", "万海": "WANHAI",
        }
        return table.get(name, name)

    def _parse_dg_cell(self, cell: str):
        cell = (cell or "").strip()
        if not cell or cell in ("-", "—"):
            return None
        dg = DGSurcharge()
        dg.format_type = "unified"
        m = re.findall(r"(\d+(?:\.\d+)?)", cell)
        if m:
            if len(m) >= 2:
                dg.dg_20 = float(m[0])
                dg.dg_40 = float(m[1])
            elif len(m) == 1:
                dg.dg_20 = float(m[0])
                dg.dg_40 = float(m[0])
        if not dg.dg_20 and not dg.dg_40:
            return None
        dg.note = cell
        return dg


register_parser(MarkdownTableParser())