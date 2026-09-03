# -*- coding: utf-8 -*-
"""
port_resolver.py - 港口名/代码 互转工具

数据源 (按优先级):
  1. assets/ports.json (889 个港口的 UN/LOCODE 字典, 含中文/英文/国家)
  2. rate_io.PORT_ALIAS (硬编码常用别名, 100+ 条, 含 BKK/PKG 等非官方短码)

支持输入:
  - 5 字符 UN/LOCODE: CNSHA / THBKK / MYPKG
  - 4 字符短码 (少见): VNQU
  - 中文港口名: 上海 / 曼谷 / 林查班 / 巴生港 / 雅加达
  - 英文港口名: SHANGHAI / BANGKOK / LAEM CHABANG / PORT KLANG / JAKARTA
  - 别名短码: BKK / PKG / JKT / SIN / HKG

返回 (un_locode, confidence, source, original):
  - un_locode: 5 (或 4) 字符代码; 找不到返回 ""
  - confidence: "high" (精确匹配) / "medium" (模糊/别名) / "low" (子串) / "" (未找到)
  - source: "ports.json" / "PORT_ALIAS" / "" (未找到)
  - original: 原始输入字符串

使用:
  from port_resolver import PortResolver
  r = PortResolver()
  code, conf, src, orig = r.resolve("曼谷")
  # ("THBKK", "high", "ports.json", "曼谷")

  name_en = r.code_to_en_name("THBKK")  # "BANGKOK"
  name_cn = r.code_to_cn_name("THBKK")  # "" (xlsx 里没填中文)
  info = r.code_to_info("CNSHA")        # {en_name, cn_name, country, ...}
"""
import json
import os
import re
import sys
from typing import Optional, Dict, Tuple, List

# 默认 ports.json 路径 (skill 部署后会变成 /app/skills/dg-rate-query/assets/ports.json)
DEFAULT_PORTS_JSON = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "assets", "ports.json",
)


class PortResolver:
    _instance = None

    def __new__(cls, *args, **kwargs):
        # 单例 (避免重复加载 173KB JSON)
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, ports_json_path: Optional[str] = None):
        if hasattr(self, "_initialized") and self._initialized:
            return
        self._initialized = True
        self.path = ports_json_path or DEFAULT_PORTS_JSON
        self.ports = []  # list of dict
        self.by_code = {}  # code -> dict
        self.cn_index = {}  # cn_name (normalized) -> code
        self.en_index = {}  # en_name (UPPER) -> code
        self.alias_index = {}  # 额外别名 -> code (合并 PORT_ALIAS)
        self._data_source = "unloaded"
        self._load()

    def _load(self):
        # 1. 优先从飞书 Bitable (tbl1MBPsIYBZPCcW, 2026-07-20 Phase 2 新表) 加载, fallback 到 ports.json
        try:
            from lark_port_source import get_lark_source
            src = get_lark_source()
            self.ports = src.load()
            self._data_source = src.source
        except Exception as e:
            print(f"[port_resolver] lark source failed: {e}, fallback to JSON", file=sys.stderr)
            self.ports = []
            self._data_source = "error"

        # 2026-07-18 P0-A3: 优先从 Bitable 加载, fallback to ports.json
        if not self.ports and os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self.ports = json.load(f)
                self._data_source = "json_only"
            except Exception as e:
                print(f"[port_resolver] JSON fallback failed: {e}", file=sys.stderr)

        # 2026-07-18 P0-A3: 已经从 Bitable 拿到数据, 但 Bitable 可能缺港口 / 缺中文名。
        # 补充策略: 把 ports.json 里 Bitable 没有的 code 补进 self.ports, 把缺的 cn_name 也补上。
        # 这样保持"Bitable 为真源", ports.json 当补丁。
        if self.ports and os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    json_ports = json.load(f)
                by_code = {p.get("code") for p in self.ports if p.get("code")}
                merged = 0
                cn_filled = 0
                for jp in json_ports:
                    code = jp.get("code")
                    if not code:
                        continue
                    # 1) 补缺失港口
                    if code not in by_code:
                        self.ports.append(jp)
                        by_code.add(code)
                        merged += 1
                    # 2) 补缺失的中文名 (已存在 code 但 cn_name 为空)
                    else:
                        for ep in self.ports:
                            if ep.get("code") == code and not (ep.get("cn_name") or "").strip() and jp.get("cn_name"):
                                ep["cn_name"] = jp["cn_name"]
                                cn_filled += 1
                                break
                if merged or cn_filled:
                    print(f"[port_resolver] merged {merged} new ports + {cn_filled} cn_names from ports.json", file=sys.stderr)
                    self._data_source = "bitable+json"
            except Exception as e:
                print(f"[port_resolver] merge with ports.json failed: {e}", file=sys.stderr)

        # 2. 构建索引
        for p in self.ports:
            code = (p.get("code") or "").strip()
            if not code:
                continue
            self.by_code[code] = p
            # 中文索引 (精确)
            cn = (p.get("cn_name") or "").strip()
            if cn:
                self.cn_index[cn] = code
            # 英文索引 (upper, 精确)
            en = (p.get("en_name") or "").strip().upper()
            if en:
                self.en_index[en] = code
                # 英文去掉 ", CHINA" / ", INDONESIA" 等后缀做别名
                for sep in [", CHINA", ", INDONESIA", ", VIETNAM",
                            ", THAILAND", ", KOREA", ", JAPAN",
                            ", MALAYSIA", ", PHILIPPINES",
                            ", BANGLADESH", ", IRAN", ", PAKISTAN",
                            ", INDIA", ", SAUDI ARABIA", ", TAIWAN"]:
                    if en.endswith(sep):
                        short = en[: -len(sep)].strip()
                        if short and short not in self.en_index:
                            self.en_index[short] = code

        # 2. 加载 PORT_ALIAS (来自 rate_io.py)
        try:
            from rate_io import PORT_ALIAS
            for alias, code in PORT_ALIAS.items():
                if not alias or not code:
                    continue
                # 别名 (大小写不敏感, 大写存)
                key = alias.strip().upper()
                if key and key not in self.alias_index:
                    self.alias_index[key] = code
        except Exception as e:
            print(f"[port_resolver] load PORT_ALIAS failed: {e}", file=sys.stderr)

    def resolve(self, raw: str) -> Tuple[str, str, str, str]:
        """把任意形式的港口字符串转成 UN/LOCODE.

        Returns: (un_locode, confidence, source, original)
        """
        if not raw:
            return ("", "", "", "")
        original = str(raw).strip()
        if not original:
            return ("", "", "", "")

        # 1) 已经是 4/5 字符代码
        code_clean = re.sub(r"\s+", "", original).upper()
        if re.match(r"^[A-Z0-9]{4,5}$", code_clean):
            if code_clean in self.by_code:
                return (code_clean, "high", "ports.json", original)
            # 可能是合法代码但字典里没有
            return (code_clean, "high", "ports.json", original)

        # 2) 精确匹配 (按输入大小写)
        if original in self.cn_index:
            return (self.cn_index[original], "high", "ports.json", original)
        if original in self.en_index:
            return (self.en_index[original], "high", "ports.json", original)
        if original in self.alias_index:
            return (self.alias_index[original], "high", "PORT_ALIAS", original)

        # 3) 大写后匹配
        up = original.upper()
        if up in self.en_index:
            return (self.en_index[up], "high", "ports.json", original)
        if up in self.alias_index:
            return (self.alias_index[up], "high", "PORT_ALIAS", original)

        # 4) 模糊匹配 (子串包含)
        # 4a) 中文子串
        for cn, code in self.cn_index.items():
            if cn and (cn in original or original in cn):
                return (code, "medium", "ports.json", original)
        # 4b) 英文子串 (输入作为整体, ports.json 英文名作为整体)
        for en, code in self.en_index.items():
            if len(en) >= 4 and (en in up or up in en):
                return (code, "medium", "ports.json", original)
        # 4c) 别名子串
        for alias, code in self.alias_index.items():
            if len(alias) >= 3 and (alias in up or up in alias):
                return (code, "medium", "PORT_ALIAS", original)

        # 5) 没找到
        return ("", "", "", original)

    def code_to_en_name(self, code: str) -> str:
        p = self.by_code.get((code or "").strip().upper())
        return (p.get("en_name") if p else "") or ""

    def code_to_cn_name(self, code: str) -> str:
        p = self.by_code.get((code or "").strip().upper())
        return (p.get("cn_name") if p else "") or ""

    def code_to_info(self, code: str) -> Dict[str, str]:
        p = self.by_code.get((code or "").strip().upper())
        if not p:
            return {}
        return {k: p.get(k, "") for k in ("en_name", "cn_name", "country", "region", "code", "switch_code", "iso_code")}

    def all_codes(self) -> List[str]:
        return sorted(self.by_code.keys())

    @property
    def data_source(self) -> str:
        """当前数据来源: 'lark' / 'json_fallback' / 'unloaded' / 'error'"""
        return self._data_source

    def reload(self):
        """强制重新加载 (跳过 in-memory cache)"""
        try:
            from lark_port_source import get_lark_source
            get_lark_source().force_refresh()
        except Exception:
            pass
        self.by_code.clear()
        self.cn_index.clear()
        self.en_index.clear()
        self.alias_index.clear()
        self._load()


# 便捷函数 (兼容旧代码 normalize_port 调用)
_default = None
def _get_default():
    global _default
    if _default is None:
        _default = PortResolver()
    return _default


def normalize_port(name: str) -> str:
    """便捷包装: 只返回 code, 没找到返回原值."""
    r = _get_default()
    code, _conf, _src, _orig = r.resolve(name)
    return code or name