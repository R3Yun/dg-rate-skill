# -*- coding: utf-8 -*-
"""Tier Guide Rate 解析器 (德翔/类似船公司 Tier Guide 报价)

格式特征:
    - Excel 抬头通常含 "德 翔 海 運 有 限 公 司" / "Tier Guide"
    - 主表头:  SVC | VESSEL | VOYAGE | <中文船名> | ETD | ATD
    - POD 块:  POD | 20GP | 40GP | 40HQ  (成 4 列循环出现在右侧区域)
    - 同 SVC 多 vessel 都使用同一组 POD 价格; OMIT = 价格待披露
    - 可能含 "CEBU (T/S KHH)" 形式的转船备注 / "BELAWAN(MYPPW)" 等承运段备注
    - 不含 DG 附加费字段 (Tier Guide 仅公布 base ocean freight)

每个 SVC 内的 POD 组会被展开为 1 条记录; 同一 SVC 在多 vessel/voyage 上重复公布同一组价格时,
取首个有完整价格的 vessel 作主记录。
"""
from typing import List, Tuple, Optional
import re
import os
import sys
import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from parsers.base import BaseRateParser, register_parser, NormalizedRateEntry


class TierGuideParser(BaseRateParser):
    name = "tier_guide"
    source_type = "tier_guide"

    HEADER_MARKERS = ("SVC", "VESSEL", "VOYAGE", "ETD", "ATD", "20GP", "40GP", "40HQ", "POD")

    SVC_AREA = {
        "CAT":  ("AUSTRALIA", "澳大利亚"),
        "CA2":  ("AUSTRALIA", "澳大利亚"),
        "CSE":  ("AUSTRALIA", "澳大利亚"),
        "CNZ":  ("NEW ZEALAND", "新西兰"),
        "CWX":  ("INDIA", "印度"),
        "EAX":  ("EAST AFRICA", "东非"),
        "IFX":  ("INDIA", "印度"),
        "CVI":  ("INDIA", "印度"),
        "IFX2": ("INDIA", "印度"),
        "CME":  ("MIDDLE EAST", "中东"),
        "KCM":  ("MALAYSIA", "马来西亚"),
        "KCM2": ("MALAYSIA", "马来西亚"),
        "CHT":  ("THAILAND", "泰国"),
        "CVT":  ("THAILAND", "泰国"),
        "CVS":  ("SINGAPORE", "新加坡"),
        "PHX":  ("PHILIPPINES", "菲律宾"),
        "PHM":  ("PHILIPPINES", "菲律宾"),
        "PNH":  ("CAMBODIA", "柬埔寨"),
        "VMS":  ("VIETNAM", "越南"),
        "VMX":  ("VIETNAM", "越南"),
        "MEX":  ("MEXICO", "墨西哥"),
        "RCX":  ("RED SEA", "红海"),
        "MSX":  ("MED", "地中海"),
    }

    POD_BLOCK_SIZE = 4

    def can_parse(self, content: str, filename: str = "") -> Tuple[float, str]:
        if not content:
            return (0.0, "")
        head = "\n".join(content.splitlines()[:50])
        filename_hit = "tier" in (filename or "").lower() or "guide" in (filename or "").lower()
        required = ("SVC", "VESSEL", "VOYAGE", "ETD", "POD", "20GP", "40HQ")
        hits = sum(1 for k in required if k in head)
        if hits < 4:
            return (0.0, "")
        base = 0.5 + 0.05 * hits
        if filename_hit:
            base += 0.2
        return (min(base, 0.95), f"tier_guide header matched ({hits}/{len(required)}) + filename={filename_hit}")

    def parse(self, content: str, filename: str = "") -> List[NormalizedRateEntry]:
        rows = self._read_tsv_rows(content)
        if not rows:
            return []
        header_idx = self._find_pod_header(rows)
        if header_idx < 0:
            return []
        pod_columns = self._pods_in_header_row(rows[header_idx])
        if not pod_columns:
            return []
        results: List[NormalizedRateEntry] = []
        current_svc = None
        seen_svc_pod = set()
        for row in rows[header_idx + 1:]:
            if not row or all(c is None for c in row):
                continue
            svc, vessel, voyage, etd = self._row_meta(row)
            if svc:
                current_svc = svc
            # vessel==OMIT 的行通常作为占位声明 (下个 voyage 公布前填写此处价格), 把 vessel 留空但保留价格
            if vessel and vessel.upper() == "OMIT":
                vessel = ""
            for pod_col in pod_columns:
                entry = self._build_entry(row, pod_col, current_svc, vessel, voyage, etd, filename)
                if not entry:
                    continue
                key = (current_svc or "", entry.pod)
                if not entry.pod:
                    continue
                if key in seen_svc_pod:
                    continue
                seen_svc_pod.add(key)
                results.append(entry)
        issue_date = self._extract_issue_date(rows[:header_idx])
        if issue_date:
            for e in results:
                if not e.valid_from:
                    e.valid_from = issue_date
        # v1.3.1 (docs/04 §8.8): entry missing valid_to -> valid_from + 30 days
        # TierGuide typical default valid period is 30 days; business can adjust later.
        DEFAULT_VALID_DAYS = 30
        for e in results:
            if not e.valid_to and e.valid_from:
                try:
                    d = datetime.datetime.strptime(e.valid_from, "%Y-%m-%d").date()
                    e.valid_to = (d + datetime.timedelta(days=DEFAULT_VALID_DAYS)).isoformat()
                except ValueError:
                    pass
        return results

    def _read_tsv_rows(self, content: str) -> List[List[Optional[str]]]:
        rows = []
        for line in content.splitlines():
            line = line.rstrip()
            if not line.strip():
                rows.append([])
                continue
            cells = line.split("\t")
            rows.append([c.strip() if isinstance(c, str) else ("" if c is None else str(c)) for c in cells])
        return rows

    def _find_pod_header(self, rows: List[List]) -> int:
        for i, row in enumerate(rows[:60]):
            cells = [str(c).strip().upper() for c in row if c not in (None, "")]
            upper_join = " ".join(cells)
            if ("POD" in upper_join and "20GP" in upper_join and "40HQ" in upper_join and "VESSEL" in upper_join):
                return i
        for i, row in enumerate(rows[:60]):
            cells = [str(c).strip().upper() for c in row if c not in (None, "")]
            upper_join = " ".join(cells)
            if "POD" in upper_join and "20GP" in upper_join and "40HQ" in upper_join:
                return i
        return -1

    def _pods_in_header_row(self, header_row: List) -> List[int]:
        cols = []
        n = len(header_row)
        i = 0
        while i + 3 < n:
            pod = (header_row[i] or "").strip().upper() if i < n else ""
            gp20 = (header_row[i + 1] or "").strip().upper() if i + 1 < n else ""
            gp40 = (header_row[i + 2] or "").strip().upper() if i + 2 < n else ""
            gphq = (header_row[i + 3] or "").strip().upper() if i + 3 < n else ""
            if pod == "POD" and gp20 == "20GP" and gp40 == "40GP" and gphq == "40HQ":
                cols.append(i)
                i += 4
            else:
                i += 1
        return cols

    def _row_meta(self, row: List) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
        if len(row) < 6:
            return (None, None, None, None)
        svc = (row[0] or "").strip().upper()
        vessel = (row[1] or "").strip()
        voyage = (row[2] or "").strip()
        etd_cell = row[4] if len(row) > 4 else None
        return (svc or None, vessel or None, voyage or None, etd_cell)

    def _build_entry(self, row, pod_col, svc, vessel, voyage, etd, filename):
        if pod_col + 3 >= len(row):
            return None
        pod_raw = (row[pod_col] or "").strip()
        if not pod_raw or pod_raw.upper() == "OMIT":
            return None
        pod_code = self.normalize_port(pod_raw)
        if not pod_code:
            pod_code = pod_raw

        of_20 = self._parse_price_cell(row[pod_col + 1])
        of_40 = self._parse_price_cell(row[pod_col + 2])
        of_40hq = self._parse_price_cell(row[pod_col + 3])

        if of_20 is None and of_40 is None and of_40hq is None:
            return None

        def _clean(v):
            if v is None:
                return None
            try:
                fv = float(v)
                return int(fv) if abs(fv - int(fv)) < 1e-9 else fv
            except Exception:
                return None
        of_20 = _clean(of_20)
        of_40 = _clean(of_40)
        of_40hq = _clean(of_40hq)

        entry = self.new_entry(filename)
        entry.pol = self._resolve_pol(svc)
        entry.pol_name = ""
        entry.pod = pod_code or pod_raw
        entry.pod_name = pod_raw
        entry.carrier = self._resolve_carrier(svc)
        entry.carrier_name = vessel or ""
        entry.rate_type = "FCL3.1"
        entry.rol, _ = self.SVC_AREA.get(svc or "", ("", ""))
        entry.rod = ""
        entry.freight_rate_type = ""
        entry.container_type = "GP"
        entry.contract_no = ""
        entry.cabin_status = ""
        entry.vat_cost = ""
        entry.vat_sell = ""
        entry.vessel = vessel or ""
        entry.voyage = voyage or ""
        entry.etd = self.parse_loose_date(etd) if etd is not None else ""
        entry.eta = ""
        entry.of_20 = of_20
        entry.of_40 = of_40
        entry.of_40hq = of_40hq
        entry.of_20nor = None
        entry.of_40nor = None
        entry.of_45 = None
        entry.tt_days = None
        entry.frequency = ""
        entry.direct = "T" if self._looks_transit(pod_raw) else "F"
        entry.via_port = self._extract_via(pod_raw)
        entry.currency = "USD"
        entry.status = ""
        entry.valid_from = ""
        entry.valid_to = ""
        entry.remark = self._build_remark(svc, vessel, etd, pod_raw)
        entry.source_type = self.source_type
        entry.parser = self.name
        entry.confidence = 0.85
        entry.raw_excerpt = "\t".join((row[pod_col + k] or "") for k in range(4))
        return entry


    @staticmethod
    def parse_loose_date(s) -> str:
        """Normalize a date cell (datetime/date/string with optional time) to YYYY-MM-DD."""
        if s is None:
            return ""
        if isinstance(s, datetime.datetime):
            return s.strftime("%Y-%m-%d")
        if isinstance(s, datetime.date):
            return s.strftime("%Y-%m-%d")
        st = str(s).strip()
        if not st:
            return ""
        patterns = [
            (re.compile(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:[ T](\d{1,2}):(\d{1,2})(?::(\d{1,2}))?)?"), lambda m: (int(m.group(1)), int(m.group(2)), int(m.group(3)))),
            (re.compile(r"^(\d{4})(\d{2})(\d{2})(?:\s+\d{1,2}:\d{1,2}:?\d{0,2})?"), lambda m: (int(m.group(1)), int(m.group(2)), int(m.group(3)))),
        ]
        for pat, conv in patterns:
            m = pat.match(st)
            if m:
                try:
                    y, mo, d = conv(m)
                    return datetime.date(y, mo, d).strftime("%Y-%m-%d")
                except Exception:
                    return ""
        return ""

    @staticmethod
    def _parse_price_cell(cell) -> Optional[float]:
        if cell is None:
            return None
        s = str(cell).strip()
        if not s or s == "/" or s.upper() == "OMIT" or s.upper() == "N/A":
            return None
        return BaseRateParser.parse_price(s)

    @staticmethod
    def _resolve_pol(svc: Optional[str]) -> str:
        return "CNSHA"

    @staticmethod
    def _resolve_carrier(svc: Optional[str]) -> str:
        return "TSL"

    @staticmethod
    def _looks_transit(pod_raw: str) -> bool:
        u = pod_raw.upper()
        return ("(" in u and "T/S" in u.upper()) or "/" in u

    @staticmethod
    def _extract_via(pod_raw: str) -> str:
        u = pod_raw.upper()
        m = re.search(r"\(?T/?S\s*([A-Z]{3,5})\)?", u)
        if m:
            return m.group(1)
        m = re.search(r"\(([^()]{1,12})\)", pod_raw)
        if m:
            return m.group(1)
        return ""

    @staticmethod
    def _build_remark(svc, vessel, etd, pod_raw):
        bits = []
        if svc:
            bits.append(f"SVC={svc}")
        if etd:
            etd_str = TierGuideParser.parse_loose_date(etd)
            if etd_str:
                bits.append(f"ETD={etd_str}")
        if vessel:
            bits.append(f"vessel={vessel}")
        if "(" in pod_raw:
            bits.append(f"pod_note={pod_raw}")
        return "; ".join(bits)

    @staticmethod
    def _extract_issue_date(head_rows: List[List]) -> str:
        for row in head_rows:
            for i, c in enumerate(row):
                if c is None:
                    continue
                if isinstance(c, str) and c.strip().lower() == "issue date":
                    for c2 in row[i + 1:]:
                        v = TierGuideParser.parse_loose_date(c2)
                        if v:
                            return v
                v = TierGuideParser.parse_loose_date(c)
                if v:
                    return v
        return ""


register_parser(TierGuideParser())