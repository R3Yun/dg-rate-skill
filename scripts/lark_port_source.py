"""
lark_port_source.py - 从飞书多维表格「港口代码」表加载港口数据

源数据: 飞书 Bitable `tbl1MBPsIYBZPCcW` (base: Eje8bWtVdaPPPosu0GQcPclQnut, 2026-07-20 Phase 2 新表)
字段映射 (新表 15 字段 -> ports.json 12 内部字段):
  UN_LOCODE       -> code (5 字符 UN/LOCODE)
  Port_EN         -> en_name
  Port_CN         -> cn_name
  Country_CN      -> country
  Route_CN        -> region
  Is_Main_Port    -> base_port (select: 是/否)
  Swap_Code       -> switch_code
  Country_ISO     -> iso_code
  City_Code       -> city_code
  City_Name       -> city_name
  Port_Requirement -> port_requirement
  Seq             -> seq
  (Country_EN / Route_EN / Port_Type 新表多出 3 字段, 不入 ports.json)

特性:
  - 内存缓存 (TTL 默认 1 小时)
  - 失败 fallback 到 assets/ports.json
  - 提供 force_refresh() 强制重载

使用:
  from lark_port_source import LarkPortSource
  src = LarkPortSource()
  records = src.load()  # list[dict], 格式与 ports.json 一致
"""
import json
import os
import subprocess
import sys
from typing import List, Dict, Optional

# 默认配置 (skill 部署后会变成 /app/skills/dg-rate-query/scripts/)
DEFAULT_LARK_BIN = os.environ.get(
    "LARK_BIN",
    "/home/node/.openclaw/workspace/bin/lark"
)
DEFAULT_BASE_TOKEN = os.environ.get(
    "LARK_PORTS_BASE_TOKEN",
    "Eje8bWtVdaPPPosu0GQcPclQnut"
)
DEFAULT_TABLE_ID = os.environ.get(
    "LARK_PORTS_TABLE_ID",
    "tbl1MBPsIYBZPCcW"  # 2026-07-20 Phase 2 新表 (替换已删的 tblU3fajZ6ysxre1)
)
DEFAULT_FALLBACK_JSON = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "assets", "ports.json"
)

# 本地业务港口主数据 (D79, 2026-08-27): 由 业务常用表 + CargoWare + 飞书表 Route_CN 合并生成.
# 运行时优先读本地文件 (秒级, 离线), 飞书表降级为兜底/同步源.
# 可通过 DG_PORTS_SOURCE=local|lark 切换 (默认 local).
DEFAULT_MASTER_JSON = os.environ.get(
    "DG_PORTS_MASTER_JSON",
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..", "assets", "biz_ports_master.json"
    )
)

# 本地主数据字段 -> ports.json 内部字段
MASTER_FIELD_MAP = {
    "code": "code",
    "un_locode": "un_locode",
    "en_name": "en_name",
    "cn_name": "cn_name",
    "country": "country",
    "country_en": "country_en",
    "country_iso": "iso_code",
    "route_cn": "region",       # 33 线名 -> region (与飞书 Route_CN 语义一致)
    "region": "region_cw",      # CargoWare 粗略区域, 额外保留
    "source": "master_source",
}

# 字段映射 (Bitable 字段名 -> ports.json 内部名)
FIELD_MAP = {
    # 新表 tbl1MBPsIYBZPCcW 字段名 -> ports.json 内部字段名 (2026-07-20 Phase 2)
    "UN_LOCODE": "code",
    "Port_EN": "en_name",
    "Port_CN": "cn_name",
    "Country_CN": "country",
    "Route_CN": "region",
    "Is_Main_Port": "base_port",
    "Swap_Code": "switch_code",
    "Country_ISO": "iso_code",
    "City_Code": "city_code",
    "City_Name": "city_name",
    "Port_Requirement": "port_requirement",
    "Seq": "seq",
    # 新表多出 3 字段 (Country_EN / Route_EN / Port_Type) 不参与映射
}


class LarkPortSource:
    """从飞书 Bitable 加载港口数据"""

    def __init__(
        self,
        lark_bin: str = DEFAULT_LARK_BIN,
        base_token: str = DEFAULT_BASE_TOKEN,
        table_id: str = DEFAULT_TABLE_ID,
        fallback_json: str = DEFAULT_FALLBACK_JSON,
        master_json: str = DEFAULT_MASTER_JSON,
        ttl_seconds: int = 3600,
    ):
        self.lark_bin = lark_bin
        self.base_token = base_token
        self.table_id = table_id
        self.fallback_json = fallback_json
        self.master_json = master_json
        self.ttl_seconds = ttl_seconds
        self._cache: Optional[List[Dict]] = None
        self._cache_time: float = 0
        self._source: str = "unloaded"

    def _load_local_master(self) -> Optional[List[Dict]]:
        """读本地业务港口主数据 (biz_ports_master.json), 字段映射到 ports.json 格式.

        失败返回 None (调用方继续走 Lark/fallback).
        """
        if not os.path.exists(self.master_json):
            return None
        try:
            with open(self.master_json, "r", encoding="utf-8") as f:
                raw = json.load(f)
            records = []
            for item in raw:
                if not isinstance(item, dict):
                    continue
                entry = {}
                for k, v in item.items():
                    entry[MASTER_FIELD_MAP.get(k, k)] = v
                if entry.get("code"):
                    records.append(entry)
            return records
        except Exception as e:
            print(f"[lark_port_source] local master load failed: {e}", file=sys.stderr)
            return None

    def load(self, force_refresh: bool = False) -> List[Dict]:
        """加载港口列表, 返回 [{code, en_name, cn_name, ...}, ...]

        D79 (2026-08-27) 数据源优先级:
           1. 本地主数据 biz_ports_master.json (DG_PORTS_SOURCE=local 默认, 秒级离线)
           2. 飞书 Bitable (Lark API, 分页全拉)
           3. ports.json (fallback)
        """
        import time
        now = time.time()
        if not force_refresh and self._cache is not None and (now - self._cache_time) < self.ttl_seconds:
            return self._cache

        source_pref = os.environ.get("DG_PORTS_SOURCE", "local")

        # 1) 本地主数据 (默认优先, 秒级离线)
        if source_pref != "lark":
            local = self._load_local_master()
            if local:
                self._cache = local
                self._cache_time = now
                self._source = "local_master"
                return local

        # 2) 尝试 Lark API (本地缺失/损坏时兜底)
        try:
            records = self._fetch_from_lark()
            if records:
                self._cache = records
                self._cache_time = now
                self._source = "lark"
                return records
        except Exception as e:
            print(f"[lark_port_source] Lark fetch failed: {e}", file=sys.stderr)

        # 3) fallback to JSON
        try:
            if os.path.exists(self.fallback_json):
                with open(self.fallback_json, "r", encoding="utf-8") as f:
                    self._cache = json.load(f)
                    self._cache_time = now
                    self._source = "json_fallback"
                    return self._cache
        except Exception as e:
            print(f"[lark_port_source] JSON fallback failed: {e}", file=sys.stderr)

        # 3) 都没有
        return []

    def _fetch_from_lark(self) -> List[Dict]:
        """调用 lark CLI API 拉取所有记录"""
        all_records = []
        page_token = None
        page = 0
        while True:
            page += 1
            params = {"page_size": 200}
            if page_token:
                params["page_token"] = page_token
            p = json.dumps(params)
            r = subprocess.run(
                [
                    self.lark_bin, "api", "GET",
                    f"/open-apis/bitable/v1/apps/{self.base_token}/tables/{self.table_id}/records",
                    "--params", p, "--format", "json"
                ],
                capture_output=True, text=True, timeout=30
            )
            if r.returncode != 0:
                raise RuntimeError(f"lark cli failed: {r.stderr}")
            d = json.loads(r.stdout)
            if not d.get("ok"):
                raise RuntimeError(f"lark api error: {d.get('error')}")
            items = d.get("data", {}).get("items", [])
            all_records.extend(items)
            if not d.get("data", {}).get("has_more"):
                break
            page_token = d["data"].get("page_token")
            if not page_token:
                break
            if page > 20:
                break  # safety

        # 转换为 ports.json 格式
        result = []
        for rec in all_records:
            fields = rec.get("fields", {})
            entry = {}
            for lark_field, internal_field in FIELD_MAP.items():
                v = fields.get(lark_field)
                if isinstance(v, str):
                    v = v.strip()
                entry[internal_field] = v if v != "" else None
            # 仅保留有 UN代码的记录
            if entry.get("code"):
                result.append(entry)
        return result

    def force_refresh(self) -> List[Dict]:
        """强制从 Lark 重新加载 (跳过缓存)"""
        return self.load(force_refresh=True)

    @property
    def source(self) -> str:
        """当前数据来源: 'lark' / 'json_fallback' / 'unloaded'"""
        return self._source


# 便捷单例
_instance = None
def get_lark_source() -> LarkPortSource:
    global _instance
    if _instance is None:
        _instance = LarkPortSource()
    return _instance