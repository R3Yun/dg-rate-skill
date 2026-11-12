# -*- coding: utf-8 -*-
"""
运价数据标准结构定义: NormalizedRateEntry / DGSurcharge
"""

from dataclasses import dataclass, field, asdict
from typing import List, Optional, Dict


@dataclass
class DGSurcharge:
    """DG附加费条目，支持三种格式: unified/by_class/by_pg"""
    format_type: str = "unified"
    dg_20: Optional[float] = None
    dg_40: Optional[float] = None
    dg_40hq: Optional[float] = None
    by_class: Dict[str, tuple] = field(default_factory=dict)
    by_pg: Dict[str, tuple] = field(default_factory=dict)
    note: str = ""

    @staticmethod
    def from_dict(d: dict) -> "DGSurcharge":
        if d is None:
            return DGSurcharge()
        dg = DGSurcharge()
        for k, v in d.items():
            if hasattr(dg, k):
                setattr(dg, k, v)
        return dg

    def get_dg_rate(self, dg_class=None, pg=None, ct="20GP"):
        idx = 0 if ct in ("20GP", "20NOR") else (1 if ct in ("40GP", "40NOR") else 2)
        def pick(t):
            return t[idx] if t and len(t) > idx else None
        if self.format_type == "unified":
            return pick((self.dg_20, self.dg_40, self.dg_40hq))
        if self.format_type == "by_class" and dg_class:
            key = str(dg_class)
            for k, v in self.by_class.items():
                if key in k.split(",") or key == k:
                    return pick(v)
        if self.format_type == "by_pg" and pg:
            pgu = pg.upper().replace(" ", "")
            for k, v in self.by_pg.items():
                if k.upper() in pgu or pgu in k.upper():
                    return pick(v)
        return None

    def to_remark(self) -> str:
        parts = []
        def fmt(v):
            return str(int(v)) if v is not None and v == int(v) else str(v)
        if self.format_type == "unified" and (self.dg_20 or self.dg_40):
            parts.append(f"DG{fmt(self.dg_20)}/{fmt(self.dg_40)}")
        elif self.format_type == "by_class":
            for cls, t in self.by_class.items():
                if len(t) >= 2:
                    parts.append(f"{cls}类DG{fmt(t[0])}/{fmt(t[1])}")
                else:
                    parts.append(f"{cls}类DG{fmt(t[0])}")
        elif self.format_type == "by_pg":
            for pg, t in self.by_pg.items():
                if len(t) >= 2:
                    parts.append(f"{pg} DG{fmt(t[0])}/{fmt(t[1])}")
                else:
                    parts.append(f"{pg} DG{fmt(t[0])}")
        if self.note:
            parts.append(self.note)
        return " ".join(parts)


@dataclass
class NormalizedRateEntry:
    """所有Parser统一输出此结构"""
    pol: str = ""
    pol_name: str = ""
    pod: str = ""
    pod_name: str = ""
    pc: str = ""                  # P/C 是 P0 字段；未提供时必须保持为空，不能默认 Both
    carrier: str = ""
    carrier_name: str = ""
    rate_no: str = ""               # 运价编号 (如 NO.001)
    booking_agent: str = ""
    rate_type: str = "FCL3.1"     # 2026-07-11 v3: 运价类型 (Rate Type) - 可选字段
    rol: str = ""                 # 2026-07-11 v3: 起运区域 (ROL)
    rod: str = ""                 # 2026-07-11 v3: 目的区域 (ROD)
    freight_rate_type: str = ""   # 2026-07-11 v3: Freight Rate Type
    container_type: str = "GP"    # 2026-07-11 v3: Container Type (GP/NOR)
    contract_no: str = ""         # 2026-07-11 v3: Contract No (Col21)
    cabin_status: str = ""        # 2026-07-11 v3: 舱位状态 Cabin Status (Col AE)
    vat_cost: str = ""            # 2026-07-11 v3: 税率 Cost (VAT) (Col AC)
    vat_sell: str = ""            # 2026-07-11 v3: 税率 Sell (VAT) (Col AD)
    vessel: str = ""              # 2026-07-11 v3: 船名 Vessel Name (Col5)
    voyage: str = ""              # 2026-07-11 v3: 航次 Voyage (Col6)
    etd: str = ""                 # 2026-07-11 v3: ETD (Col7)
    eta: str = ""                 # 2026-07-11 v3: ETA (Col8)
    of_20: Optional[float] = None
    of_40: Optional[float] = None
    of_40hq: Optional[float] = None
    of_20nor: Optional[float] = None
    of_40nor: Optional[float] = None
    of_45: Optional[float] = None
    # Q2 (2026-07-21): DG附加费, 与 20GP/40GP/40HQ O/F 对称
    dg_20: Optional[float] = None
    dg_40: Optional[float] = None
    dg_40hq: Optional[float] = None
    dg_surcharges: List[DGSurcharge] = field(default_factory=list)
    tt_days: Optional[int] = None
    frequency: str = ""
    direct: str = ""
    via_port: str = ""
    contract_no: str = ""
    currency: str = "USD"
    status: str = ""  # 飞书多维表格的"状态"（仅 待补充/已生效）
    valid_from: str = ""
    valid_to: str = ""
    ams: Optional[float] = None
    ens: Optional[float] = None
    ows_note: str = ""
    free_time: Optional[int] = None
    remark: str = ""
    source_file: str = ""
    source_type: str = ""
    parsed_at: str = ""
    parser: str = ""
    confidence: float = 1.0
    warnings: List[str] = field(default_factory=list)
    raw_excerpt: str = ""
    data_source: str = ""        # 数据来源: 船公司公告/货代报价/客户询价
    source_format: str = ""      # 来源格式: xlsx/xls/csv/txt/image/pdf
    raw_excerpt: str = ""

    @staticmethod
    def from_dict(d: dict) -> "NormalizedRateEntry":
        if d is None:
            return NormalizedRateEntry()
        e = NormalizedRateEntry()
        for k, v in d.items():
            if k == "dg_surcharges" and isinstance(v, list):
                setattr(e, k, [DGSurcharge.from_dict(x) if isinstance(x, dict) else x for x in v])
            elif hasattr(e, k):
                setattr(e, k, v)
        return e

    def to_dict(self):
        return asdict(self)

    def summary(self) -> str:
        o = f"{self.of_20}/{self.of_40}/{self.of_40hq}" if self.of_20 else "无价格"
        return f"{self.pol}->{self.pod} | {self.carrier} | {o} | {self.valid_from}~{self.valid_to}"


# 港口代码映射表（常用）
PORT_ALIAS = {
    # 中国
    "上海": "CNSHA", "上海港": "CNSHA", "SHANGHAI": "CNSHA", "CNSHA": "CNSHA",
    # v3.10.5.1: WGP/外高桥 = 上海外高桥港区, 视为 CNSHA
    "外高桥": "CNSHA", "外高桥港": "CNSHA", "WGP": "CNSHA", "SHA WGP": "CNSHA", "WAIGAOQIAO": "CNSHA",
    "宁波": "CNNGB", "宁波港": "CNNGB", "NINGBO": "CNNGB",
    "深圳": "CNSZN", "盐田": "CNSHK", "盐田港": "CNSHK",
    "青岛": "CNTAO", "QINGDAO": "CNTAO", "青岛港": "CNTAO",
    "天津": "CNTSN", "TIANJIN": "CNTSN", "天津港": "CNTSN",
    "广州": "CNGZH", "GUANGZHOU": "CNGZH",
    "厦门": "CNXMN", "XIAMEN": "CNXMN", "厦门港": "CNXMN",
    "大连": "CNDLC", "DALIAN": "CNDLC", "大连港": "CNDLC",
    "连云港": "CNLYG", "LIANYUNGANG": "CNLYG",
    "福州": "CNFOC", "FUZHOU": "CNFOC",
    "珠海": "CNZUH", "ZHUHAI": "CNZUH",
    "香港": "HKHKG", "HONG KONG": "HKHKG",
    "高雄": "TWKHH", "基隆": "TWKEL",
    # 东南亚
    "曼谷": "THBKK", "BANGKOK": "THBKK", "BKK": "THBKK", "PAT": "THBKK", "BMT": "THBKK",
    "林查班": "THLCH", "LAEM CHABANG": "THLCH", "LCH": "THLCH", "LCB": "THLCH",
    "胡志明": "VNSGN", "HO CHI MINH": "VNSGN", "HCM": "VNSGN", "CAT LAI": "VNSGN",
    "海防": "VNHPH", "HAIPHONG": "VNHPH",
    "新加坡": "SGSIN", "SINGAPORE": "SGSIN", "SIN": "SGSIN",
    "巴生": "MYPKG", "PORT KELANG": "MYPKG", "PORT KLANG": "MYPKG", "PKG": "MYPKG", "巴生港": "MYPKG", "巴生北港": "MYPKG",
    "西港": "MYPEN", "PENANG": "MYPEN",
    "巴西古当": "MYPGU", "PASIR GUDANG": "MYPGU",
    "雅加达": "IDJKT", "JAKARTA": "IDJKT", "JKT": "IDJKT",
    "泗水": "IDSUB", "SURABAYA": "IDSUB",
    "三宝垄": "IDSRG", "SEMARANG": "IDSRG",
    "马尼拉": "PHMNL", "MANILA": "PHMNL", "MNL": "PHMNL",
    "马尼拉北": "PHMNN",
    "宿务": "PHCEB", "CEBU": "PHCEB",
    "西哈努克": "KHKOS", "SIHANOUKVILLE": "KHKOS",
    "金边": "KHPPI", "PHNOM PENH": "KHPPI",
    "仰光": "MMRGN", "YANGON": "MMRGN",
    # 韩国/日本
    "釜山": "KRPUS", "BUSAN": "KRPUS", "PUSAN": "KRPUS",
    "仁川": "KRINC", "INCHEON": "KRINC",
    "东京": "JPTYO", "TOKYO": "JPTYO",
    "横滨": "JPYOK", "YOKOHAMA": "JPYOK",
    # WS-163 (2026-08-31): Bug 3 补充 — 酒田 (山形县, Sakata) → JPSKT (UN/LOCODE)
    "酒田": "JPSKT", "SAKATA": "JPSKT",
    "神户": "JPUKB", "KOBE": "JPUKB",
    "大阪": "JPOSA", "OSAKA": "JPOSA",
    "名古屋": "JPNGO", "NAGOYA": "JPNGO",
    # 中东/印巴
    "迪拜": "AEDXB", "DUBAI": "AEDXB",
    "杰贝阿里": "AEJEA", "JEBEL ALI": "AEJEA",
    # WS-163 (2026-08-31): Bug 3 修复 — JEB ALI (业务常用简称, 缺中间 EL) → AEJEA
    "JEB ALI": "AEJEA",
    "阿布扎比": "AEAUH", "ABU DHABI": "AEAUH",
    "达曼": "SADMM", "DAMMAM": "SADMM",
    "吉达": "SAJED", "JEDDAH": "SAJED",
    "Nhava Sheva": "INNSA",
    # WS-163 (2026-08-31): Bug 5 修复 — 那瓦什瓦 NAVASHEVA 中文/英文别名 → INNSA
    # 业务常用写法: "那瓦什瓦 NAVASHEVA" / "那瓦什瓦" / "NHAVA SHEVA" (官方 en_name)
    "那瓦什瓦": "INNSA", "NAVASHEVA": "INNSA", "NHAVA SHEVA": "INNSA",
    "孟买": "INBOM", "MUMBAI": "INBOM",
    "钦奈": "INMAA", "CHENNAI": "INMAA",
    "卡拉奇": "PKKHI", "KARACHI": "PKKHI",
    # 印度 Hazira 私营港（Adani 集团）
    "Adani Hazira": "INHZA", "ADANI HAZIRA": "INHZA", "HAZIRA": "INHZA",
    "科伦坡": "LKCMB", "COLOMBO": "LKCMB",
    # 欧洲
    "鹿特丹": "NLRTM", "ROTTERDAM": "NLRTM",
    "汉堡": "DEHAM", "HAMBURG": "DEHAM",
    "安特卫普": "BEANR", "ANTWERP": "BEANR",
    "菲利克斯托": "GBFXT", "FELIXSTOWE": "GBFXT",
    "勒阿弗尔": "FRLEH", "LE HAVRE": "FRLEH",
    "不来梅港": "DEBRV", "BREMERHAVEN": "DEBRV",
    # 地中海
    "比雷埃夫斯": "GRPIR", "PIRAEUS": "GRPIR",
    "瓦伦西亚": "ESVLC", "VALENCIA": "ESVLC",
    "热那亚": "ITGOA", "GENOA": "ITGOA",
    "拉斯佩齐亚": "ITSPE", "LA SPEZIA": "ITSPE",
    "巴塞罗那": "ESBCN", "BARCELONA": "ESBCN",
    "马耳他": "MTMAR", "MALTA": "MTMAR",
    "塞德港": "EGPSD", "PORT SAID": "EGPSD",
    # 美洲
    "洛杉矶": "USLAX", "LOS ANGELES": "USLAX",
    "长滩": "USLGB", "LONG BEACH": "USLGB",
    "纽约": "USNYC", "NEW YORK": "USNYC",
    "萨凡纳": "USSAV", "SAVANNAH": "USSAV",
    "西雅图": "USSEA", "SEATTLE": "USSEA",
    "奥克兰": "USOAK", "OAKLAND": "USOAK",
    "诺福克": "USNFK", "NORFOLK": "USNFK",
    "休斯顿": "USHOU", "HOUSTON": "USHOU",
    "桑托斯": "BRSSZ", "SANTOS": "BRSSZ",
    # 非洲
    "德班": "ZADUR", "DURBAN": "ZADUR",
    "开普敦": "ZACPT", "CAPE TOWN": "ZACPT",
    "蒙巴萨": "KEMBA", "MOMBASA": "KEMBA",
    "达累斯萨拉姆": "TZDAR", "DAR ES SALAAM": "TZDAR",
    # 大洋洲
    "悉尼": "AUSYD", "SYDNEY": "AUSYD",
    "墨尔本": "AUMEL", "MELBOURNE": "AUMEL",
    "布里斯班": "AUBNE", "BRISBANE": "AUBNE",
    "弗里曼特尔": "AUFRE", "FREMANTLE": "AUFRE",
    "奥克兰NZ": "NZAKL", "AUCKLAND": "NZAKL",
}


# 2026-07-11 v3: 关键/可选/迁移 字段分类定义
# 业务来源: 用户 2026-07-11 确认, 与 docs/04-rate-management.md §表1.1 对齐
#
# CRITICAL_FIELDS (6 个): 缺失即拒绝入库
#   - 必须为非空字符串 / 非 None / 日期格式正确
#   - 缺失时 preflight 返回 critical_missing, write-lark 拒收
#   - D80 (2026-08-28): +booking_agent 订舱代理 — CargoWare 导入必填,
#     解析时由订舱口主数据按 carrier 自动匹配中文名称; 匹配不到 → 问询业务.
#
# OPTIONAL_FIELDS (20 个): 列必须存在, 单元格可空
#   - CargoWare 模板中这些字段必须有列, 但可空
#   - preflight 报告 optional_missing (仅提示, 不拒收)
#
# AUTO_FIELDS: 由 Agent 自动填充, 不视为缺失
#   - status / parsed_at / import_time 等
# (v3.7+: TRANSITION_REQUIRED 已删除, 审核人字段已移除, 状态迁移不再需审核人)

CRITICAL_FIELDS = [
    ("pol",         "起运港 POL"),
    ("pod",         "目的港 POD"),
    ("pc",          "P/C 提货方式 (Both/CY/Port)"),
    ("valid_from",  "有效期起 Valid fm"),
    ("valid_to",    "有效期止 Valid to"),
    ("booking_agent", "订舱代理 (Booking Agent)"),  # D80: P1 → P0 升级
]

OPTIONAL_FIELDS = [
    # (attr, 中文标签, CargoWare 模板列)
    # 飞书字段顺序 (2026-07-21 实测 + 用户截图): 与用户当前飞书 UI 顺序一致
    # 39-field = 5 CRITICAL + 28 OPTIONAL + 3 AUTO + 1 auto_number + 1 数据来源 + 1 原文件附件
    # 例外: 有效期起/有效期止 已在 _write_batch 强制相邻 (OPTIONAL_FIELDS 不含 CRITICAL)
    ("rate_type",       "运价类型 (Rate Type)",     "第1行"),
    ("rol",             "起运区域 (ROL)",            "第2行"),
    ("rod",             "目的区域 (ROD)",            "第3行"),
    ("pol_name",        "起运港全称 (POL Name)",      ""),
    ("pod_name",        "目的港全称 (POD Name)",      ""),
    ("via_port",        "VIA中转港 (Via Port)",      "Col VIA"),
    ("direct",          "直航 (Direct/Transit)",     "Col DIR"),
    ("carrier",         "船公司 (Carrier)",          "Col 船公司"),
    ("frequency",       "班期 (Frequency)",          "Col 班期"),
    ("tt_days",         "航程(天) (Transit Time)",   "Col TT"),
    ("vessel",          "船名 (Vessel Name)",         "Col VN"),
    ("voyage",          "航次 (Voyage)",              "Col VY"),
    ("etd",             "ETD (预计开航日)",      "Col ETD"),
    ("eta",             "ETA (预计到港日)",      "Col ETA"),
    ("booking_agent",   "订舱代理 (Booking Agent)",  "Col BA"),
    ("of_20",           "20GP O/F(USD)",             "Col 20GP"),
    ("of_40",           "40GP O/F(USD)",             "Col 40GP"),
    ("of_40hq",         "40HQ O/F(USD)",             "Col 40HQ"),
    ("of_20nor",        "20NOR O/F(USD)",            "Col 20NOR"),
    ("of_40nor",        "40NOR O/F(USD)",            "Col 40NOR"),
    ("of_45",           "45尺 O/F(USD)",             "Col 45尺"),
    ("dg_40",           "40GP DG(USD)",              "Col DG40"),
    ("dg_40hq",         "40HQ DG(USD)",              "Col DG40HQ"),
    ("dg_20",           "20GP DG(USD)",              "Col DG20"),
    ("ens",             "ENS费用 (ENS)",              "Col ENS"),
    ("ams",             "AMS费用 (AMS)",              "Col AMS"),
    ("free_time",       "免柜期(天) (Free Time)",     "Col FT"),
    ("contract_no",     "合约号 (Contract No)",       "Col 合约"),
    ("ows_note",        "超重备注 (OWS Note)",       "Col OWS"),
    ("remark",          "备注 (Remark)",             "Col 备注"),
]


# ============================================================
# v3.8 (2026-07-19): 三级漏斗字段分级 P0/P1/P2/AUTO
# 决策来源: docs/decisions/20260718-p1-field-completion-3tier.md
# 对应文档: docs/04-rate-management.md 表1.1
#
# - P0 阻塞 (CRITICAL_FIELDS, 5个): 缺失即拒收，不入库
# - P1 必问 (P1_FIELDS, 2个字段 + 1个派生检查): 缺了写入「待补充」状态
# - P2 提示 (P2_FIELDS, 9个): 缺了写入正常状态，Agent 回复里提示
# - AUTO (AUTO_FIELDS): 自动填，不视为缺失
# ============================================================

# P1 必问字段 (1 个字段 + 1 个派生检查 "≥1 价格")
# 缺了 → 写入「待补充」状态，阻塞审核流
# 补字段: dg-rate-query update-record <record_id> --field k=v
# (D80: booking_agent 已升 P0, 从 P1 移除)
P1_FIELDS = [
    # (attr, 中文标签, 补充规则)
    ("carrier",       "船公司（备注无时）", "可从备注提取，或问业务人员"),
    # "≥1 价格" 不是单字段, 单独由 has_at_least_one_price() 检查
]

# P2 提示字段 (9 个)
# 缺了 → 写入正常状态, Agent 回复里提示业务人员「现在补或后补」
# 补字段: dg-rate-query write-record <record_id> --merge <新字段 JSON>
P2_FIELDS = [
    # (attr, 中文标签)
    ("currency",    "币种（默认 USD）"),
    ("rol",         "起运区域 (ROL)"),
    ("rod",         "目的区域 (ROD)"),
    ("frequency",   "班期 (Frequency)"),
    ("vessel",      "船名 (Vessel Name)"),
    ("voyage",      "航次 (Voyage)"),
    ("etd",         "ETD"),
    ("eta",         "ETA"),
    ("contract_no", "合约号 (Contract No)"),
    # DG 附加费不在 rate_io, 单独由 DG 附加费表管理 (v3.8 不强制问)
]

# 价格字段 (6 个) — P1 派生检查 "≥1 价格" 用
PRICE_FIELDS = ["of_20", "of_40", "of_40hq", "of_20nor", "of_40nor", "of_45"]


def has_at_least_one_price(entry) -> bool:
    """检查 entry 是否至少有一个价格字段非空 (P1 派生检查)"""
    for attr in PRICE_FIELDS:
        v = getattr(entry, attr, None)
        if v is not None and (not isinstance(v, str) or v.strip()):
            return True
    return False




def get_p1_missing(entry) -> list:
    """v3.8 新增: 返回 entry 中缺失的 P1 必问字段名列表 (中文标签)

    包含:
      - carrier / booking_agent 单字段缺失
      - 6 个 PRICE_FIELDS 全空 ("≥1 价格" 派生检查)
    """
    missing = []
    for attr, label, _rule in P1_FIELDS:
        v = getattr(entry, attr, None)
        if v is None or (isinstance(v, str) and not v.strip()):
            missing.append(label)
    if not has_at_least_one_price(entry):
        missing.append("≥1 价格 (20/40/40HQ/20NOR/40NOR/45 至少一个)")
    return missing


def get_p2_missing(entry) -> list:
    """v3.8 新增: 返回 entry 中缺失的 P2 提示字段名列表 (中文标签)

    不阻塞, 仅用于 Agent 回复里提示业务人员「现在补或后补」
    """
    missing = []
    for attr, label in P2_FIELDS:
        v = getattr(entry, attr, None)
        if v is None or (isinstance(v, str) and not v.strip()):
            missing.append(label)
    return missing


def classify_entry(entry) -> dict:
    """v3.8 新增: 一站式分类 entry, 返回 {p0_missing, p1_missing, p2_missing, status}

    status:
      - "p0_blocked" : 任一 P0 字段缺失, 拒收
      - "p1_downgrade": P0 全, 但有 P1 字段缺失, 写入「待补充」
      - "complete"    : P0+P1 都齐, 正常写入 (P2 缺失仅 warnings)
    """
    p0 = get_missing_critical_fields(entry)
    p1 = get_p1_missing(entry)
    p2 = get_p2_missing(entry)
    if p0:
        # P0 阻塞: 整条不写入, P1/P2 缺失无意义 (清空)
        status = "p0_blocked"
        p1 = []
        p2 = []
    elif p1:
        status = "p1_downgrade"
    else:
        status = "complete"
    return {
        "status": status,
        "p0_missing": p0,
        "p1_missing": p1,
        "p2_missing": p2,
    }

def get_missing_critical_fields(entry) -> list:
    """返回 entry 中缺失的 P0 阻塞字段名列表 (中文标签)

    v3 改: 仅校验 5 个 P0 字段, 不再强制价格字段
    v3.8 改: 函数保留作为 compatibility wrapper, 推荐用 classify_entry()
    用法:
      from rate_io import get_missing_critical_fields
      missing = get_missing_critical_fields(entry)
      if missing:
          print(f"缺少 P0 字段: {missing}, 请业务人员补充")
    """
    missing = []
    for attr, label in CRITICAL_FIELDS:
        v = getattr(entry, attr, None)
        if v is None or (isinstance(v, str) and not v.strip()):
            missing.append(label)
    return missing




# v3.7+: TRANSITION_REQUIRED 已删除 (审核人字段已移除)
AUTO_FIELDS = [
    # v3.7+: import_user / import_user_name 已移除
    "status",            # 完整记录默认 '已生效'，P1 缺失为 '待补充'
    "parsed_at",         # 旰与时间戳
    "import_time",       # v3.7+ 新增: 导入时间 (DateTime, Asia/Shanghai)
]



def get_missing_optional_fields(entry) -> list:
    """返回 entry 中缺失的可选字段名列表 (中文标签)
    v3 新增: 仅作为提示, 不阻止入库
    """
    missing = []
    for attr, label, _cw_col in OPTIONAL_FIELDS:
        v = getattr(entry, attr, None)
        if v is None or (isinstance(v, str) and not v.strip()):
            missing.append(label)
    return missing




def is_critical_complete(entry) -> bool:
    """关键字段是否齐全 (5 个)"""
    return len(get_missing_critical_fields(entry)) == 0


def preflight_summary(entry) -> dict:
    """v3 新增, v3.8 扩展: 一次性返回 critical/p1/p2 三类缺失情况

    Returns:
        {
            "ok": bool,                    # P0 关键字段全部齐全 (可入库)
            "critical_missing": list,      # P0 字段缺失 (拒收) — 同 p0_missing
            "optional_missing": list,      # OPTIONAL 字段缺失 (兼容保留, 22 个)
            "p1_missing": list,            # v3.8 新增: P1 必问字段缺失 (写入待补充)
            "p2_missing": list,            # v3.8 新增: P2 提示字段缺失 (正常入库 + warnings)
            "status": str,                 # v3.8 新增: p0_blocked / p1_downgrade / complete
            "warning": str,                # 人类可读的摘要
        }
    """
    crit = get_missing_critical_fields(entry)
    opt = get_missing_optional_fields(entry)
    p1 = get_p1_missing(entry)
    p2 = get_p2_missing(entry)
    if crit:
        warning = "⚠️ P0 阻塞字段缺失, 整条不写入: " + ", ".join(crit)
        status = "p0_blocked"
    elif p1:
        warning = f"⚠️ P1 必问字段缺失 {len(p1)} 个 (写入「待补充」): " + ", ".join(p1)
        status = "p1_downgrade"
    elif p2:
        warning = f"💡 P2 提示字段缺失 {len(p2)} 个 (正常入库, Agent 提示后补): " + ", ".join(p2)
        status = "complete"
    else:
        warning = "✅ 全部 P0/P1 字段齐全"
        status = "complete"
    return {
        "ok": len(crit) == 0,
        "critical_missing": crit,
        "optional_missing": opt,
        "p1_missing": p1,
        "p2_missing": p2,
        "status": status,
        "warning": warning,
    }


# 2026-07-16 v3.1: ROL/ROD 自动填充 (从 POL/POD country 反查)
#
# 业务背景: FCL 表 49 条记录 ROL/ROD 字段全部空白,
#          因 parse 时未基于 POL/POD 自动计算. 飞书表 4→13 选项已扩展,
#          现在 parse 后强制 enrich.
#
# 设计要点:
#   1. country → region 硬编码 (不依赖 ports.json 不可靠的 region 字段)
#   2. 同时覆盖 中国/香港/澳门/台湾 → "中国", 与 FCL 选项对齐
#   3. 返回区域字符串, 调用方校验是否在 ROL_OPTIONS / ROD_OPTIONS 中
COUNTRY_TO_REGION = {
    # 中国 + 港澳台
    "中国": "中国", "中国香港": "中国", "中国澳门": "中国", "中国台湾": "中国",
    "香港": "中国", "澳门": "中国", "台湾": "中国",
    # 日韩
    "韩国": "日韩", "朝鲜": "日韩", "日本": "日韩", "蒙古": "日韩",
    # 东南亚 (11 国)
    "越南": "东南亚", "泰国": "东南亚", "缅甸": "东南亚", "柬埔寨": "东南亚",
    "老挝": "东南亚", "马来西亚": "东南亚", "新加坡": "东南亚",
    "印度尼西亚": "东南亚", "印尼": "东南亚", "菲律宾": "东南亚",
    "文莱": "东南亚", "东帝汶": "东南亚",
    # 南亚 (印巴)
    "印度": "印巴", "巴基斯坦": "印巴", "孟加拉国": "印巴", "孟加拉": "印巴",
    "斯里兰卡": "印巴", "尼泊尔": "印巴", "不丹": "印巴", "马尔代夫": "印巴",
    # 中东
    "阿联酋": "中东", "沙特阿拉伯": "中东", "沙特": "中东", "伊朗": "中东",
    "伊拉克": "中东", "科威特": "中东", "卡塔尔": "中东", "巴林": "中东",
    "阿曼": "中东", "也门": "中东", "约旦": "中东", "以色列": "中东",
    "黎巴嫩": "中东", "叙利亚": "中东",
    # 地中海
    "土耳其": "地中海", "希腊": "地中海", "意大利": "地中海", "西班牙": "地中海",
    "葡萄牙": "地中海", "马耳他": "地中海", "塞浦路斯": "地中海", "埃及": "地中海",
    # 欧洲
    "荷兰": "欧洲", "德国": "欧洲", "法国": "欧洲", "英国": "欧洲",
    "比利时": "欧洲", "爱尔兰": "欧洲", "丹麦": "欧洲", "瑞典": "欧洲",
    "芬兰": "欧洲", "挪威": "欧洲", "波兰": "欧洲", "捷克": "欧洲",
    "斯洛伐克": "欧洲", "匈牙利": "欧洲", "奥地利": "欧洲", "瑞士": "欧洲",
    "罗马尼亚": "欧洲", "保加利亚": "欧洲", "俄罗斯": "远东（俄罗斯）", "乌克兰": "欧洲",
    "白俄罗斯": "欧洲", "立陶宛": "欧洲", "拉脱维亚": "欧洲", "爱沙尼亚": "欧洲",
    "斯洛文尼亚": "欧洲", "克罗地亚": "欧洲", "塞尔维亚": "欧洲",
    # 北美
    "美国": "北美", "加拿大": "北美", "墨西哥": "北美",
    # 南美
    "巴西": "南美", "阿根廷": "南美", "智利": "南美", "秘鲁": "南美",
    "哥伦比亚": "南美", "委内瑞拉": "南美", "厄瓜多尔": "南美",
    "乌拉圭": "南美", "巴拉圭": "南美", "玻利维亚": "南美",
    # 非洲
    "南非": "非洲", "尼日利亚": "非洲", "肯尼亚": "非洲", "埃塞俄比亚": "非洲",
    "加纳": "非洲", "坦桑尼亚": "非洲", "乌干达": "非洲", "摩洛哥": "非洲",
    "阿尔及利亚": "非洲", "突尼斯": "非洲", "利比亚": "非洲", "苏丹": "非洲",
    "安哥拉": "非洲", "莫桑比克": "非洲", "津巴布韦": "非洲", "赞比亚": "非洲",
    "塞内加尔": "非洲", "科特迪瓦": "非洲", "喀麦隆": "非洲", "刚果": "非洲",
    # 大洋洲
    "澳大利亚": "澳新", "新西兰": "澳新", "巴布亚新几内亚": "澳新",
    "斐济": "澳新", "所罗门群岛": "澳新",
}

# 2026-07-16 v3.1: LOCODE 前 2 字符 → 国家兜底映射 (ISO 3166-1 alpha-2)
#
# 目的: ports.json 未收录的港口 (如 KRINC / JPTYO / IDJKT / INHZA) 是
#       PORT_ALIAS 里别名-only 条目, 没有 country 字段. 用 LOCODE 前 2 字符
#       (ISO 3166-1 alpha-2) 兜底推断. 覆盖业务常用约 80 个国家.
LOCODE_PREFIX_TO_COUNTRY = {
    # East Asia
    "CN": "中国", "HK": "中国", "MO": "中国", "TW": "中国",
    "JP": "日本", "KR": "韩国", "KP": "朝鲜", "MN": "蒙古",
    # Southeast Asia
    "TH": "泰国", "VN": "越南", "MY": "马来西亚", "SG": "新加坡",
    "ID": "印度尼西亚", "PH": "菲律宾", "MM": "缅甸", "KH": "柬埔寨",
    "LA": "老挝", "BN": "文莱", "TL": "东帝汶",
    # South Asia
    "IN": "印度", "PK": "巴基斯坦", "BD": "孟加拉国", "LK": "斯里兰卡",
    "NP": "尼泊尔", "BT": "不丹", "MV": "马尔代夫",
    # Middle East
    "AE": "阿联酋", "SA": "沙特阿拉伯", "IR": "伊朗", "IQ": "伊拉克",
    "KW": "科威特", "QA": "卡塔尔", "BH": "巴林", "OM": "阿曼", "YE": "也门",
    "JO": "约旦", "IL": "以色列", "LB": "黎巴嫩", "SY": "叙利亚",
    # Mediterranean
    "TR": "土耳其", "GR": "希腊", "IT": "意大利", "ES": "西班牙",
    "PT": "葡萄牙", "MT": "马耳他", "CY": "塞浦路斯", "EG": "埃及",
    # Europe
    "NL": "荷兰", "DE": "德国", "FR": "法国", "GB": "英国", "UK": "英国",
    "BE": "比利时", "IE": "爱尔兰", "DK": "丹麦", "SE": "瑞典", "FI": "芬兰",
    "NO": "挪威", "PL": "波兰", "CZ": "捷克", "SK": "斯洛伐克",
    "HU": "匈牙利", "AT": "奥地利", "CH": "瑞士", "RO": "罗马尼亚",
    "BG": "保加利亚", "RU": "俄罗斯", "UA": "乌克兰", "BY": "白俄罗斯",
    "LT": "立陶宛", "LV": "拉脱维亚", "EE": "爱沙尼亚",
    "SI": "斯洛文尼亚", "HR": "克罗地亚", "RS": "塞尔维亚",
    # North America
    "US": "美国", "CA": "加拿大", "MX": "墨西哥",
    # South America
    "BR": "巴西", "AR": "阿根廷", "CL": "智利", "PE": "秘鲁",
    "CO": "哥伦比亚", "VE": "委内瑞拉", "EC": "厄瓜多尔",
    "UY": "乌拉圭", "PY": "巴拉圭", "BO": "玻利维亚",
    # Africa
    "ZA": "南非", "NG": "尼日利亚", "KE": "肯尼亚", "ET": "埃塞俄比亚",
    "GH": "加纳", "TZ": "坦桑尼亚", "UG": "乌干达", "MA": "摩洛哥",
    "DZ": "阿尔及利亚", "TN": "突尼斯", "LY": "利比亚", "SD": "苏丹",
    "AO": "安哥拉", "MZ": "莫桑比克", "ZW": "津巴布韦", "ZM": "赞比亚",
    "SN": "塞内加尔", "CI": "科特迪瓦", "CM": "喀麦隆", "CG": "刚果",
    # Oceania
    "AU": "澳大利亚", "NZ": "新西兰", "PG": "巴布亚新几内亚",
    "FJ": "斐济", "SB": "所罗门群岛",
}

# FCL ROL / ROD 选项 (与飞书表 schema 对齐)
FCL_REGION_OPTIONS = (
    # 东亚
    "中国", "日韩",
    # 东南亚 + 印巴
    "东南亚", "印巴",
    # 西亚 + 中亚 + 俄罗斯远东
    "中东", "中亚", "远东（俄罗斯）",
    # 地中海 + 欧洲
    "地中海", "欧洲", "北欧", "东欧", "西亚",
    # 南北美 + 加勒比
    "南美", "中美洲", "北美", "加勒比",
    # 大洋洲 + 非洲
    "澳新", "非洲",
)


# 2026-07-18: 已填 region 值的别名映射 (LLM 输出 -> FCL canonical)
# 背景: A3 E2E 发现可可能填入非规范字符串 ("俄罗斯远东"), 而 enrich_regions_dict
#       "已有不覆盖"逻辑跳过, 导致 schema not_found。
# 数据规则: 已知别名强制映射到 canonical; 在映射表内 -> canonical; 都不匹配 -> 保留原值
#       (调用方 _normalize_select_values 会把不在 FCL_REGION_OPTIONS 的清空)
REGION_VALUE_ALIASES = {
    # 俄罗斯远东族
    "俄罗斯远东": "远东（俄罗斯）",
    "俄远东": "远东（俄罗斯）",
    "远东俄罗斯": "远东（俄罗斯）",
    "俄罗斯": "远东（俄罗斯）",
    # 北美 / 美西美东
    "美国": "北美",
    "美西": "北美",
    "美东": "北美",
    "美西美东": "北美",
    "美国内陆": "北美",
    # 中南美
    "南美洲": "南美",
    "中美": "中美洲",
    # 大洋洲
    "大洋洲": "澳新",
    "澳洲": "澳新",
    # 东南亚别名 (防御性)
    "东南亚地区": "东南亚",
    # 日韩
    "韩日": "日韩",
    # D79 (2026-08-27): 港口代码表线名 (D72 region 字段, 如 中国港口/东南亚线/韩国线)
    # → FCL 区域 canonical (CargoWare 模板 ROD/飞书 select 只接受 FCL_REGION_OPTIONS).
    # 此前线名直接写出 → CargoWare 导入报"目的区域错误"; 飞书写库也会被 select 校验清空.
    "中国港口": "中国",
    "中国": "中国",
    "东南亚线": "东南亚",
    "韩国线": "日韩",
    "日本线": "日韩",
    "日本基本港": "日韩",
    "日本偏港": "日韩",
    "中东线": "中东",
    "欧洲线": "欧洲",
    "欧洲内陆": "欧洲",
    "北欧线": "北欧",
    "印巴线": "印巴",
    "澳新线": "澳新",
    "南太平洋": "澳新",
    "波罗的海": "北欧",
    "红海": "中东",
    "亚洲": "东南亚",
}


def normalize_region_value(v):
    """把任意 region 字符串 normalize 到 FCL canonical。

    规则:
      1. 空值 -> ""
      2. 已在 FCL_REGION_OPTIONS 内 -> 原样返回
      3. 在 REGION_VALUE_ALIASES 内 -> 映射到 canonical
      4. 都不匹配 -> 原样返回 (外层 _normalize_select_values 会清掉)
    """
    if v is None:
        return ""
    s = str(v).strip()
    if not s:
        return ""
    if s in FCL_REGION_OPTIONS:
        return s
    if s in REGION_VALUE_ALIASES:
        return REGION_VALUE_ALIASES[s]
    return s


# D73 (2026-08-20): 中文 region -> 英文区域名 (DISPLAY-ONLY).
# 覆盖 FCL_REGION_OPTIONS 全部 18 个 canonical 值 + ports.json/lark 表常见
# 线名/别名变体 (东南亚线/韩国线/日本线/中国港口/中东线/欧洲线/印巴线/澳新线/
# 美东/美西/红海/波罗的海/日本基本港/日本偏港/亚洲/南太平洋/南美洲/欧洲内陆 等).
# ⚠️ 仅用于 preview 展示 (rod_en/rol_en), 永不写入飞书 select 字段
#    (FCL select 只接受中文 canonical, 见 FCL_REGION_OPTIONS).
REGION_EN_NAMES = {
    # --- FCL_REGION_OPTIONS canonical (18) ---
    "中国": "China",
    "日韩": "Japan/Korea",
    "东南亚": "Southeast Asia",
    "印巴": "India/Pakistan",
    "中东": "Middle East",
    "中亚": "Central Asia",
    "远东（俄罗斯）": "Russian Far East",
    "地中海": "Mediterranean",
    "欧洲": "Europe",
    "北欧": "Northern Europe",
    "东欧": "Eastern Europe",
    "西亚": "West Asia",
    "南美": "South America",
    "中美洲": "Central America",
    "北美": "North America",
    "加勒比": "Caribbean",
    "澳新": "Australia/New Zealand",
    "非洲": "Africa",
    # --- ports.json region 线名/细分变体 ---
    "中国港口": "China",
    "东南亚线": "Southeast Asia",
    "韩国线": "Korea",
    "日本线": "Japan",
    "日本基本港": "Japan",
    "日本偏港": "Japan",
    "中东线": "Middle East",
    "欧洲线": "Europe",
    "欧洲内陆": "Europe",
    "印巴线": "India/Pakistan",
    "澳新线": "Australia/New Zealand",
    "美东": "United States East Coast",
    "美西": "United States West Coast",
    "美国内陆": "United States Inland",
    "波罗的海": "Baltic",
    "红海": "Red Sea",
    "亚洲": "Asia",
    "南太平洋": "South Pacific",
    "南美洲": "South America",
    # --- REGION_VALUE_ALIASES 输入侧别名 (防御性, alias 可能未 normalize 就传入) ---
    "俄罗斯远东": "Russian Far East",
    "俄远东": "Russian Far East",
    "远东俄罗斯": "Russian Far East",
    "俄罗斯": "Russia",
    "美国": "United States",
    "美西美东": "United States",
    "中美": "Central America",
    "大洋洲": "Oceania",
    "澳洲": "Australia",
    "东南亚地区": "Southeast Asia",
    "韩日": "Japan/Korea",
}


def region_en(region_cn: str) -> str:
    """中文 region -> 英文区域名 (DISPLAY-ONLY, D73 2026-08-20).

    大小写/前后空白 tolerant: " 中东 " -> "Middle East".
    未收录 -> "" (调用方自行决定展示空串还是原值).

    ⚠️ 不用于飞书 select 写入 (FCL select 只接受中文 canonical).
    """
    if region_cn is None:
        return ""
    s = str(region_cn).strip()
    if not s:
        return ""
    return REGION_EN_NAMES.get(s, "")


def port_to_region(port_code: str) -> str:
    """根据 UN/LOCODE 5 码查 region (中文).

    流程:
      1. 调 PortResolver.code_to_info 拿 country 字段
      2. 查 COUNTRY_TO_REGION
      3. 找不到返回 "" (caller 决定是否保留为空)

    Args:
        port_code: 例如 "CNSHA" / "THBKK" / "INHZA"

    Returns:
        region 字符串 (FCL_REGION_OPTIONS 内 / 或 "")

    Examples:
        >>> port_to_region("CNSHA")
        '中国'
        >>> port_to_region("THBKK")
        '东南亚'
        >>> port_to_region("INHZA")
        '印巴'
        >>> port_to_region("XXXXX")
        ''
    """
    if not port_code or not isinstance(port_code, str):
        return ""
    code = port_code.strip().upper()
    if not code:
        return ""
    try:
        from port_resolver import PortResolver
        info = PortResolver().code_to_info(code)
    except Exception:
        return ""
    if not info:
        # ports.json 未收录 → 回退到 LOCODE 前 2 字符 (ISO 3166-1 alpha-2)
        country = LOCODE_PREFIX_TO_COUNTRY.get(code[:2], "")
        if country:
            return COUNTRY_TO_REGION.get(country, "")
        return ""
    # D72 修复 (2026-08-11): 优先用港口代码表 region 字段 (33 线名, 如 中国港口/东南亚线)
    # 业务方要求 ROL/ROD 用港口代码表的线名, 而非国家级映射 (中国/东南亚)
    region = (info.get("region") or "").strip()
    if region:
        return region
    country = (info.get("country") or "").strip()
    if not country:
        # info 存在但 country 空 → 同样回退到 LOCODE 前缀
        country = LOCODE_PREFIX_TO_COUNTRY.get(code[:2], "")
    if not country:
        return ""
    return COUNTRY_TO_REGION.get(country, "")


def enrich_regions(item) -> None:
    """RateItem 自动填 ROL/ROD (in-place 修改).

    逻辑:
      - rol 为空 且 pol 有值 → rol = port_to_region(pol)
      - rod 为空 且 pod 有值 → rod = port_to_region(pod)
      - 已有值不覆盖 (parser 给出的优先)

    Args:
        item: RateItem 实例 (或类 dict 对象, 需支持 setattr)
    """
    pol = (getattr(item, "pol", "") or "").strip()
    pod = (getattr(item, "pod", "") or "").strip()
    rol = (getattr(item, "rol", "") or "").strip()
    rod = (getattr(item, "rod", "") or "").strip()
    if not rol and pol:
        new_rol = normalize_region_value(port_to_region(pol))
        if new_rol:
            item.rol = new_rol
    if not rod and pod:
        new_rod = normalize_region_value(port_to_region(pod))
        if new_rod:
            item.rod = new_rod


def enrich_regions_dict(d: Dict) -> Dict:
    """dict 形式的 enrich (供 lark_rate_writer 写入前用).

    2026-07-18 v3.4.3:
      Step 1: 先 normalize 已有值 (alias -> canonical, e.g. "俄罗斯远东" -> "远东（俄罗斯）")
      Step 2: 按 "原本空才补" 逻辑填值 (pol/pod -> port_to_region)
      Step 3: canonical 内或原值保留 (用户明确填的合理值不覆盖)

    Note:
      在 FCL_REGION_OPTIONS / REGION_VALUE_ALIASES 都没有的值 (e.g. "Caribbean")
      原样保留 — 外层 _normalize_select_values 会把不在 FCL_REGION_OPTIONS 的清空。
    """
    if not isinstance(d, dict):
        return d
    pol = (d.get("pol") or "").strip()
    pod = (d.get("pod") or "").strip()
    # Step 1: alias normalize 已填值
    if d.get("rol"):
        d["rol"] = normalize_region_value(d["rol"])
    if d.get("rod"):
        d["rod"] = normalize_region_value(d["rod"])
    # Step 2: 原本空才 port_to_region 补 (D79: 结果 normalize 到 FCL canonical —
    #         port_to_region 可能返回港口线名如 韩国线/中国港口, CargoWare ROD/飞书 select 不认)
    if not (d.get("rol") or "").strip() and pol:
        new_rol = normalize_region_value(port_to_region(pol))
        if new_rol:
            d["rol"] = new_rol
    if not (d.get("rod") or "").strip() and pod:
        new_rod = normalize_region_value(port_to_region(pod))
        if new_rod:
            d["rod"] = new_rod
    return d
