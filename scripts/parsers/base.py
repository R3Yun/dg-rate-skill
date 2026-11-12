# -*- coding: utf-8 -*-
"""Parser基类与注册机制"""
import os
import re
import datetime
from typing import List, Optional, Tuple
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from rate_io import NormalizedRateEntry, DGSurcharge, PORT_ALIAS
from port_resolver import PortResolver


class BaseRateParser:
    """所有Parser继承此类"""
    name: str = "base"
    source_type: str = ""  # source_type标签

    def can_parse(self, content: str, filename: str = "") -> Tuple[float, str]:
        """返回 (置信度0-1, 识别原因)。子类必须实现。"""
        return (0.0, "")

    def parse(self, content: str, filename: str = "") -> List[NormalizedRateEntry]:
        """解析文本内容，返回NormalizedRateEntry列表"""
        raise NotImplementedError

    def new_entry(self, filename="") -> NormalizedRateEntry:
        e = NormalizedRateEntry()
        e.source_file = filename
        e.source_type = self.source_type
        e.parser = self.name
        e.parsed_at = datetime.datetime.now().isoformat(timespec="seconds")
        return e

    @staticmethod
    def normalize_port(name: str) -> str:
        """港口名称/别名 -> UN/LOCODE 5码代码
        数据源优先级: assets/ports.json (889 条) -> rate_io.PORT_ALIAS (100+ 条) -> 原样返回
        用于多源运价文件解析后的 POL/POD 字段标准化，导出 cargoware 模板时使用
        """
        if not name: return ""
        try:
            code, conf, src, orig = PortResolver().resolve(name)
            if code:
                return code
        except Exception:
            pass
        # 兜底: 仅用 PORT_ALIAS (向后兼容 / 沙盒无 ports.json 时)
        n = name.strip().upper()
        if n in PORT_ALIAS: return PORT_ALIAS[n]
        n2 = name.strip()
        if n2 in PORT_ALIAS: return PORT_ALIAS[n2]
        for k, v in PORT_ALIAS.items():
            if k.upper() in n or n in k.upper(): return v
        return name.strip()

    @staticmethod
    def parse_price(s: str) -> Optional[float]:
        """从字符串中提取数字价格，支持 450/USD450/$450/450.0 等格式"""
        if s is None: return None
        m = re.search(r"[\d]+(?:\.[\d]+)?", str(s).replace(",", ""))
        return float(m.group()) if m else None

    @staticmethod
    def parse_date(s: str) -> Tuple[str, str]:
        """从文本中解析起止日期，支持多种格式
        返回 (valid_from YYYY-MM-DD, valid_to YYYY-MM-DD)
        """
        if not s: return ("", "")
        # 2026-05-20~2026-06-20
        m = re.search(r"(\d{4})[-/\.](\d{1,2})[-/\.](\d{1,2})\s*[~\-到至]\s*(\d{4})[-/\.](\d{1,2})[-/\.](\d{1,2})", s)
        if m:
            return (f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}",
                    f"{m.group(4)}-{int(m.group(5)):02d}-{int(m.group(6)):02d}")
        # 20260520-20260620
        m = re.search(r"(\d{4})(\d{2})(\d{2})\s*[~\-]\s*(\d{4})(\d{2})(\d{2})", s)
        if m:
            return (f"{m.group(1)}-{m.group(2)}-{m.group(3)}",
                    f"{m.group(4)}-{m.group(5)}-{m.group(6)}")
        # Valid fm/to YYYY/MM/DD
        m = re.search(r"(\d{4})[/\-](\d{1,2})[/\-](\d{1,2})", s)
        if m:
            d = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
            return (d, "")
        return ("", "")

    @staticmethod
    def parse_dg_remark(text: str) -> List[DGSurcharge]:
        """从Remark文本中解析DG附加费，支持三种格式
        返回DGSurcharge列表（可能多条）
        """
        results = []
        if not text: return results
        t = text.upper().replace("：",":")

        # 格式1: DG250/500 或 DG250/500/500
        m = re.search(r"DG\s*(?:USD|RMB|CNY)?\s*(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)(?:\s*/\s*(\d+(?:\.\d+)?))?", t)
        if m:
            dg = DGSurcharge(format_type="unified",
                           dg_20=float(m.group(1)), dg_40=float(m.group(2)),
                           dg_40hq=float(m.group(3)) if m.group(3) else float(m.group(2)))
            results.append(dg)
            return results  # 统一格式优先返回

        # 格式2: 按Class分档 (如 "3类包装 USD550/750" "9类 250/500")
        class_pattern = re.findall(r"(\d+(?:\s*,\s*\d+)*)\s*类(?:包装)?\s*(?:USD|RMP)?\s*(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)", t)
        if class_pattern:
            dg = DGSurcharge(format_type="by_class")
            for cls, p20, p40 in class_pattern:
                dg.by_class[cls.strip()] = (float(p20), float(p40))
            # 找剩余note
            results.append(dg)
            return results

        # 格式3: 按PG分档 (如 "PGI:250/500 PGII:200/400 PGIII:150/300" 或 "PG1/PG2/PG3")
        pg_pattern = re.findall(r"PG\s*([I123]+)\s*[:：]?\s*(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)", t)
        if pg_pattern:
            dg = DGSurcharge(format_type="by_pg")
            pg_map = {"1":"PGI","2":"PGII","3":"PGIII","I":"PGI","II":"PGII","III":"PGIII"}
            for pg, p20, p40 in pg_pattern:
                k = pg_map.get(pg.upper(), f"PG{pg}")
                dg.by_pg[k] = (float(p20), float(p40))
            results.append(dg)
            return results

        return results


# 注册所有parser
PARSER_REGISTRY: List[BaseRateParser] = []

def register_parser(parser: BaseRateParser):
    PARSER_REGISTRY.append(parser)

def auto_select_parser(content: str, filename: str="") -> Optional[BaseRateParser]:
    """根据内容/文件名自动选择置信度最高的parser"""
    best = None
    best_score = 0.0
    best_reason = ""
    for p in PARSER_REGISTRY:
        score, reason = p.can_parse(content, filename)
        if score > best_score:
            best_score, best, best_reason = score, p, reason
    return (best, best_score, best_reason) if best else (None, 0, "")
