# -*- coding: utf-8 -*-
"""
Booking Agent 订舱代理主数据 (D80, 2026-08-28).

数据源: docs/port-master/booking_agent_master.json (部署副本: assets/booking_agent_master.json)
  来自 订舱口.xlsx (CargoWare 客商主数据, 1304 条, 排除 11 条禁用 + 合并 13 组重复代码 → 1280 唯一代码).

两条解析链:
  - 解析侧 resolve(carrier):       运价船公司 → 订舱口匹配 → 返回订舱代理中文名称 (写飞书表, P0)
  - 导出侧 resolve_code(中文名称):  订舱代理中文名称 → CargoWare 代码 (填 CargoWare 模板 Booking Agent 列)

status 语义:
  - "ok"        : 唯一确定, 返回 (名称/代码, "ok")
  - "ambiguous" : 命中多客商共用代码 (HBHY/SITC/AMHKGS), 需业务确认
  - "unmatched" : 未匹配, 需业务补充

用法:
  from booking_agent_master import get_ba_master
  master = get_ba_master()
  name, status = master.resolve("TS LINES")            # -> ("德翔航运（上海德圣船务有限公司）", "ok")
  code, status = master.resolve_code("德翔航运（上海德圣船务有限公司）")  # -> ("DXHYSHDS", "ok")
"""
import json
import os
import re
from typing import Dict, List, Optional, Tuple

_CJK_RE = re.compile(r"[\u3400-\u9fff]")

# 默认主数据路径 (skill 部署: assets/ 与脚本同目录的上一级)
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_MASTER_JSON = os.environ.get(
    "DG_BA_MASTER_JSON",
    os.path.join(_SCRIPT_DIR, "..", "assets", "booking_agent_master.json"),
)

# 多客商共用代码 (不同客商撞同一 CargoWare 代码) — 匹配到需业务确认
AMBIGUOUS_CODES = {"HBHY", "SITC", "AMHKGS"}


class BookingAgentMaster:
    """订舱口主数据: 简称/中英名 → 代码 双向索引, 进程内缓存."""

    def __init__(self, master_json: str = DEFAULT_MASTER_JSON):
        self.path = master_json
        self._short_index: Dict[str, str] = {}   # 简称 UPPER -> code
        self._en_index: Dict[str, str] = {}      # 英文名 UPPER -> code
        self._cn_index: Dict[str, str] = {}      # 中文名 -> code
        self._code_cns: Dict[str, List[str]] = {}  # code -> [中文名, ...]
        self._code_shorts: Dict[str, List[str]] = {}
        self._code_ens: Dict[str, List[str]] = {}
        self.ambiguous_codes: set = set(AMBIGUOUS_CODES)
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            print(f"[booking_agent_master] master not found: {self.path}", file=__import__("sys").stderr)
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"[booking_agent_master] load failed: {e}", file=__import__("sys").stderr)
            return
        for rec in data.get("records", []):
            code = str(rec.get("code") or "").strip()
            if not code:
                continue
            self._code_cns.setdefault(code, [])
            self._code_shorts.setdefault(code, [])
            self._code_ens.setdefault(code, [])
            for s in rec.get("shorts", []) or []:
                s = str(s).strip()
                if s:
                    self._short_index.setdefault(s.upper(), code)
                    self._code_shorts[code].append(s)
            for cn in rec.get("cns", []) or []:
                cn = str(cn).strip()
                if cn:
                    self._cn_index.setdefault(cn, code)
                    self._code_cns[code].append(cn)
            for en in rec.get("ens", []) or []:
                en = str(en).strip()
                if en:
                    self._en_index.setdefault(en.upper(), code)
                    self._code_ens[code].append(en)
        # 文件内 ambiguous 标记覆盖默认
        file_amb = data.get("ambiguous_codes") or []
        if file_amb:
            self.ambiguous_codes = {str(c).strip() for c in file_amb}

    # ---------------- 解析侧: carrier -> 订舱代理中文名称 ----------------
    def resolve(self, carrier: str) -> Tuple[str, str]:
        """运价船公司 → 订舱口匹配 → (订舱代理中文名称, status).

        status: ok / ambiguous / unmatched. ambiguous 时不返回名称 (调用方问业务).
        """
        n = str(carrier or "").strip()
        if not n:
            return "", "unmatched"
        nu = n.upper()
        code = None
        # 1) 简称精确
        if nu in self._short_index:
            code = self._short_index[nu]
        # 2) 英文名精确
        if code is None and nu in self._en_index:
            code = self._en_index[nu]
        # 3) 中文名精确
        if code is None and n in self._cn_index:
            code = self._cn_index[n]
        # 4) 中文子串 (输入 ⊂ 中文名, 如 兴亚 ⊂ 兴亚船务)
        # D94/WS-220 (2026-09-03): 加 CJK 护栏 — 只有"中文输入 ⊂ 中文名"才做子串匹配.
        # 旧实现允许纯 ASCII 船司代码 (如 OCR 服务代码 CIE) 对中文表里的英文长名做子串,
        # CIE ⊂ "SHANDONG RAINBOW AGROSCIENCES..." 误命中订舱代理 SHANDONG RAINBOW.
        if code is None and len(n) >= 2 and _CJK_RE.search(n):
            for cn, c in self._cn_index.items():
                if n in cn:
                    code = c
                    break
        # 5) 英文子串 (输入 ⊂ 英文名, ≥4 字符, 如 HEUNG-A ⊂ HEUNG-A SHIPPING)
        if code is None and len(n) >= 4:
            for en, c in self._en_index.items():
                if nu in en:
                    code = c
                    break
        if code is None:
            return "", "unmatched"
        if code in self.ambiguous_codes:
            return "", "ambiguous"
        cns = self._code_cns.get(code) or []
        name = cns[0] if cns else ""
        return name, "ok"

    # ---------------- 导出侧: 订舱代理中文名称 -> CargoWare 代码 ----------------
    def resolve_code(self, ba_name: str) -> Tuple[str, str]:
        """订舱代理名称 (中文/简称/英文) → CargoWare 代码.

        精确优先, 兼容存量数据里可能存的简称/英文名; ambiguous 代码返回 status=ambiguous.
        """
        n = str(ba_name or "").strip()
        if not n:
            return "", "unmatched"
        nu = n.upper()
        code = None
        # 1) 中文名精确
        if n in self._cn_index:
            code = self._cn_index[n]
        # 2) 简称精确 (兼容存量: 可能存了简称如 TS LINES)
        if code is None and nu in self._short_index:
            code = self._short_index[nu]
        # 3) 英文名精确
        if code is None and nu in self._en_index:
            code = self._en_index[nu]
        # 4) 已经是代码本身 (如 ASL / CUL / SITC)
        if code is None and nu in self._code_shorts:
            code = nu
        if code is None:
            # 中文子串兜底 (D94/WS-220: 同 resolve — 仅 CJK 输入才做中文子串)
            if len(n) >= 2 and _CJK_RE.search(n):
                for cn, c in self._cn_index.items():
                    if n in cn:
                        code = c
                        break
        if code is None:
            return "", "unmatched"
        if code in self.ambiguous_codes:
            return code, "ambiguous"
        return code, "ok"

    @property
    def loaded(self) -> bool:
        return bool(self._code_cns)

    @property
    def code_count(self) -> int:
        return len(self._code_cns)


# 便捷单例 (进程内缓存, 仿 lark_port_source)
_instance: Optional[BookingAgentMaster] = None


def get_ba_master() -> BookingAgentMaster:
    global _instance
    if _instance is None:
        _instance = BookingAgentMaster()
    return _instance


def reset_ba_master():
    """测试用: 重置单例."""
    global _instance
    _instance = None


if __name__ == "__main__":
    m = get_ba_master()
    print(f"master: {m.path}")
    print(f"唯一代码: {m.code_count} | ambiguous: {sorted(m.ambiguous_codes)}")
    print("\n解析侧 resolve(carrier):")
    for t in ("兴亚", "XYQD", "TS LINES", "ASL", "SNL", "China United Lines", "SITC", "YML", "HAMBURG SUD", "OOCL", "不存在的船公司"):
        name, st = m.resolve(t)
        print(f"  {t!r} -> 名称={name!r} status={st}")
    print("\n导出侧 resolve_code(中文名):")
    for t in ("兴亚船务有限公司", "德翔航运（上海德圣船务有限公司）", "ASL", "中外运集装箱运输有限公司", "SITC", "山东海丰国际航运集团有限公司"):
        code, st = m.resolve_code(t)
        print(f"  {t!r} -> 代码={code!r} status={st}")
