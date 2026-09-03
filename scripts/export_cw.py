#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CargoWare FCL3.1 模板导出工具（完整版）

功能：
  1. 从解析结果 JSON 导出（兼容老用法）
  2. 从飞书 FCL海运费表 直接拉取，导出 .xls
  3. 整合飞书 DG附加费表，按 危险品类别 写到 Remark 列
  4. 支持多维度筛选：运价编号、船公司、港口、导入时间范围
  5. 输出真正的 .xls 文件（56 列全表，与 CargoWare 模板一致）

用法：
  # 从 JSON 导出
  python export_cw.py parse_result.json -o export.xls

  # 导出空白模板
  python export_cw.py --template -o blank_template.xls

  # 从飞书多维表格直接拉取并导出
  python export_cw.py --from-feishu -o export.xls

  # DG-only 过滤
  python export_cw.py parse_result.json --dg-only -o dg_export.xls

底层命令：写 .xls 用 xlwt；读飞书用 lark-cli 通过 SSH
"""
import argparse
import datetime as _dt
import datetime
import json
import os
import subprocess
import sys
from typing import List, Dict, Any, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rate_io import NormalizedRateEntry, DGSurcharge


# ============================================================
# CargoWare FCL3.1 模板 - 完整 56 列定义
# 来自 docs/cargoware-templates/FCL_sample(20260506).xls
# ============================================================
# 列类型定义 (v3.10.6 P0-B 修复: 按列类型分写 float/datetime/str)
NUM_COLUMNS = {
    "20'", "40'", "40'HC", "20'NOR", "40'NOR", "45'",
    "Reject 20'", "Reject 40'", "Reject 40'HC", "Reject 20'NOR", "Reject 40'NOR", "Reject 45'",
    "VAT(Cost)", "VAT(Sell)", "T/T", "Free Time",
}
DATE_COLUMNS = {"Valid fm", "Valid to", "DateTypeEffective", "DateTypeExpiration", "Closing Date"}

# 默认 CargoWare 模板路径 (P0-C 修复: xlutils.copy 保留原模板格式)
# D79 (2026-08-27): 模板路径改绝对 (基于脚本位置) — 相对 cwd 在 wrapper/插件调用时
# (cwd 非项目根) 会找不到模板 → fallback legacy 全 string (日期写 ISO 文本/ROD 空) →
# CargoWare 导入报错. 脚本位于 skills/dg-rate-query/scripts/, 模板在 <根>/docs/cargoware-templates/.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TEMPLATE_PATH = os.path.normpath(os.path.join(
    _SCRIPT_DIR, "..", "..", "..", "docs", "cargoware-templates", "FCL_sample(20260506).xls"
))

CW_COLUMNS = [
    "POL",                                # 0
    "POD",                                # 1
    "VIA",                                # 2
    "Direct",                             # 3
    "Vessel Name",                        # 4
    "Voyage",                             # 5
    "ETD",                                # 6
    "ETA",                                # 7
    "Freight Rate Type",                  # 8
    "Container Type",                     # 9
    "20'",                                # 10
    "40'",                                # 11
    "40'HC",                              # 12
    "20'NOR",                             # 13
    "40'NOR",                             # 14
    "45'",                                # 15
    "Frequency",                          # 16
    "T/T",                                # 17
    "P/C",                                # 18
    "Carrier",                            # 19
    "Contract No",                        # 20
    "Outport Code",                       # 21
    "Lane Code",                          # 22
    "Booking Agent",                      # 23
    "Dock",                               # 24
    "Cargo Type",                         # 25
    "Commodity",                          # 26
    "VAT(Cost)",                          # 27
    "VAT(Sell)",                          # 28
    "Cabin Status",                       # 29
    "Reject 20'",                         # 30
    "Reject 40'",                         # 31
    "Reject 40'HC",                       # 32
    "Reject 20'NOR",                      # 33
    "Reject 40'NOR",                      # 34
    "Reject 45'",                         # 35
    "Share",                              # 36
    "Trend",                              # 37
    "Sale State",                         # 38
    "Valid fm",                           # 39
    "Valid to",                           # 40
    "DateTypeEffective",                  # 41
    "DateTypeExpiration",                 # 42
    "Remark",                             # 43
    "Free Time",                          # 44
    "ReceivingInstructions",              # 45
    "DestinationDock",                    # 46
    "TransportMode",                      # 47
    "CODE",                               # 48
    "NA",                                 # 49
    "NAC",                                # 50
    "Closing Date",                       # 51
    "AMS/ENS",                            # 52
    "Overload Remark",                    # 53
    "Custom Specs",                       # 54
    "Price Nature",                       # 55
]

# 元数据 6 行（不可调整位置）
# 行 1: 运价类型  行 2: 起运区域  行 3: 目的区域  行 4: 币种  行 5: VAT(Cost)  行 6: VAT(Sell)
META_ROW_1 = ["运价类型(Rate Type)", "FCL3.1", "", "", "说明:", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", ""]
META_ROW_2 = ["起运区域(ROL)", "中国", "", "", "1-6行的信息位置不要调整", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", ""]
META_ROW_3 = ["目的区域(ROD)", "", "", "不需要的字段，黑色列可手工在模板中删除，红色字体的列不可从表", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", ""]
META_ROW_4 = ["币种(Currency)", "USD", "", "黄色背景色的字段，必填项，不可留空", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", ""]
META_ROW_5 = ["税率Cost(VAT)", "0.0", "", "A列为空，其他列有数据将被当作注释，扫描价格时直接跳过", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", ""]
META_ROW_6 = ["税率Sell(VAT)", "0.0", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", ""]


# ============================================================
# 格式化
# ============================================================
def _fmt_int(v) -> str:
    """价格格式化为整数（无小数）。"""
    if v is None or v == "": return ""
    try: return str(int(float(v)))
    except: return str(v)


def _fmt_price(v) -> str:
    """价格格式化为保留 2 位小数（CargoWare 模板用）。"""
    if v is None or v == "": return ""
    try:
        fv = float(v)
        return str(fv) if fv == int(fv) else "{:.2f}".format(fv)
    except: return str(v)


def _fmt_date(v) -> str:
    """日期格式化为 YYYY/MM/DD（CargoWare 模板用）。"""
    if not v: return ""
    s = str(v)
    # 已是 YYYY-MM-DD
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        return s.replace("-", "/")
    return s


def format_dg_remark(entry: NormalizedRateEntry, extra_dg_list: List[Dict[str, Any]] = None) -> str:
    """将DG附加费格式化为 CargoWare Remark 格式

    按 docs/04-rate-management.md 3.2 节规则：
      - 统一格式: DG20/40
      - 按 Class 分档: <船司> <类>类 USD<rate>/<rate40>/20DG/40DG
      - 按 PG 分档: PG<num> <rate>/<rate40>
      - 末尾追加其他附加费（ENS / OWS 等）
      - 末尾追加有效期

    extra_dg_list: 从飞书 DG附加费表 拉来的关联记录（可选）
    """
    parts = []
    # 1. 原始 remark
    if entry.remark:
        stripped = entry.remark.strip().rstrip(";，, ")
        if stripped:
            parts.append(stripped)
    # 2. DG 附加费（从 entry.dg_surcharges）
    for dg in entry.dg_surcharges:
        parts.extend(_format_one_dg(dg, carrier=entry.carrier))
    # 3. DG 附加费（从飞书 DG表 拉来的关联记录）
    if extra_dg_list:
        for rec in extra_dg_list:
            dg = _dg_from_feishu_record(rec)
            if dg:
                parts.extend(_format_one_dg(dg, carrier=entry.carrier))
    # 4. 其他附加费
    if entry.ens:
        parts.append("ENS USD" + _fmt_int(entry.ens))
    if entry.ams:
        parts.append("AMS USD" + _fmt_int(entry.ams))
    if entry.ows_note:
        parts.append("OWS " + str(entry.ows_note))
    # 5. 有效期（按模板习惯）
    if entry.valid_from and entry.valid_to:
        parts.append("Valid " + (entry.valid_from or "").replace("-", "") + "-" + (entry.valid_to or "").replace("-", ""))
    return "  ".join(p for p in parts if p)


def _format_one_dg(dg, carrier=None) -> List[str]:
    """格式化单条 DG 附加费。"""
    out = []
    if dg.format_type == "unified":
        if dg.dg_20 and dg.dg_40:
            out.append("DG" + _fmt_int(dg.dg_20) + "/" + _fmt_int(dg.dg_40))
        elif dg.dg_20:
            out.append("DG" + _fmt_int(dg.dg_20))
    elif dg.format_type == "by_class":
        for cls, t in (dg.by_class or {}).items():
            if len(t) >= 2:
                prefix = (str(carrier) + " ") if carrier else ""
                out.append(prefix + str(cls) + "类 USD" + _fmt_int(t[0]) + "/" + _fmt_int(t[1]) + "/20DG/40DG")
            elif len(t) >= 1:
                prefix = (str(carrier) + " ") if carrier else ""
                out.append(prefix + str(cls) + "类 USD" + _fmt_int(t[0]))
    elif dg.format_type == "by_pg":
        for pg, t in (dg.by_pg or {}).items():
            if len(t) >= 2:
                out.append(str(pg) + " " + _fmt_int(t[0]) + "/" + _fmt_int(t[1]))
            elif len(t) >= 1:
                out.append(str(pg) + " " + _fmt_int(t[0]))
    return out


def _dg_from_feishu_record(rec: Dict[str, Any]) -> Optional[DGSurcharge]:
    """从飞书 DG表 记录构造 DGSurcharge。"""
    if not rec:
        return None
    fields = rec.get("fields", {}) or {}
    fmt = fields.get("附加费格式", "unified")
    dg = DGSurcharge(format_type=fmt)
    if fmt == "unified":
        dg.dg_20 = fields.get("20DG(USD)")
        dg.dg_40 = fields.get("40DG(USD)")
        dg.dg_40hq = fields.get("40HQ DG(USD)") or dg.dg_40
    elif fmt == "by_class":
        # 适用DG类别 是多选（["2","3","6","8","9"]）
        classes = fields.get("适用DG类别", []) or []
        # 20DG/40DG 拆给所有类别
        for c in classes:
            dg.by_class[str(c)] = (
                fields.get("20DG(USD)") or 0,
                fields.get("40DG(USD)") or 0,
            )
    elif fmt == "by_pg":
        pgs = fields.get("适用包装类", []) or []
        for p in pgs:
            dg.by_pg[str(p)] = (
                fields.get("20DG(USD)") or 0,
                fields.get("40DG(USD)") or 0,
            )
    return dg


# ============================================================
# 数据转换
# ============================================================
def build_row(entry: NormalizedRateEntry, extra_dg_list: List[Dict[str, Any]] = None) -> Dict[str, str]:
    """将 NormalizedRateEntry 转为 56 列 CSV 字段。

    WS-163 (2026-08-31): 修复 5 个 plugin bug:
      - Bug 1: P/C 不再硬编码 "Both" — 尊重 entry.pc (P0 字段, D7 闸门禁默认)
      - Bug 2: Vessel/Voyage/ETD/ETA 从 entry 字段读取 (之前全硬编码空字符串)
      - Bug 4: Direct 智能推导 — 有中转港时默认 T (transit), 数据空才留空;
               VIA 直接透传 entry.via_port (不在导出层加 KRPUS 等错误默认值)
    """
    remark = format_dg_remark(entry, extra_dg_list)
    ams_ens = ""
    if entry.ens:
        ams_ens += "ENS USD" + _fmt_int(entry.ens)
    if entry.ams:
        ams_ens += ("  " if ams_ens else "") + "AMS USD" + _fmt_int(entry.ams)
    # Bug 4: Direct 智能推导. 显式值优先, 空 + 有中转港 → T (transit), 全空才留空.
    direct_val = (entry.direct or "").strip()
    via_val = (entry.via_port or "").strip()
    if not direct_val:
        if via_val:
            direct_val = "T"
        # 否则保持空 (数据缺失, 不强加默认值, 由业务后期补)
    return {
        "POL": entry.pol or "",
        "POD": _normalize_pod(entry.pod, entry.pod_name)[0],
        "VIA": via_val,
        "Direct": direct_val,
        "Vessel Name": (entry.vessel or "").strip(),
        "Voyage": (entry.voyage or "").strip(),
        "ETD": _fmt_date(entry.etd) if entry.etd else "",
        "ETA": _fmt_date(entry.eta) if entry.eta else "",
        "Freight Rate Type": "0.0",
        "Container Type": "GP",
        "20'": _fmt_price(entry.of_20),
        "40'": _fmt_price(entry.of_40),
        "40'HC": _fmt_price(entry.of_40hq),
        "20'NOR": _fmt_price(entry.of_20nor),
        "40'NOR": _fmt_price(entry.of_40nor),
        "45'": _fmt_price(entry.of_45),
        "Frequency": entry.frequency or "",
        "T/T": str(entry.tt_days) if entry.tt_days else "",
        "P/C": entry.pc or "",
        "Carrier": entry.carrier or "",
        "Contract No": entry.contract_no or "",
        "Outport Code": "",
        "Lane Code": "",
        "Booking Agent": _get_booking_agent(entry),
        "Dock": "",
        "Cargo Type": "",
        "Commodity": "",
        "VAT(Cost)": "0.0",
        "VAT(Sell)": "0.0",
        "Cabin Status": "",
        "Reject 20'": "",
        "Reject 40'": "",
        "Reject 40'HC": "",
        "Reject 20'NOR": "",
        "Reject 40'NOR": "",
        "Reject 45'": "",
        "Share": "",
        "Trend": "",
        "Sale State": entry.status or "",  # 飞书 状态 字段，待补充/已生效；空=待人工填
        "Valid fm": _fmt_date(entry.valid_from),
        "Valid to": _fmt_date(entry.valid_to),
        "DateTypeEffective": "",
        "DateTypeExpiration": "",
        "Remark": remark,
        "Free Time": str(entry.free_time) if entry.free_time else "",
        "ReceivingInstructions": "",
        "DestinationDock": "",
        # D79 (2026-08-27): TransportMode 留空 — CargoWare 模板该列不填 (所有样本数据行
        # 均为空), 之前硬编码 "SEA" 导致 CargoWare 导入报 "TransportMode 不正确".
        "TransportMode": "",
        "CODE": "",
        "NA": "",
        "NAC": "",
        "Closing Date": "",
        "AMS/ENS": ams_ens,
        "Overload Remark": entry.ows_note or "",
        "Custom Specs": "",
        "Price Nature": "",
    }


# ============================================================
# 写出 .xls
# ============================================================
def _parse_date_value(v):
    """解析多种日期格式返回 datetime; 失败返回 None."""
    if not v:
        return None
    if hasattr(v, "year") and hasattr(v, "month") and hasattr(v, "day"):
        import datetime as _dt
        if isinstance(v, _dt.datetime):
            return v
        if isinstance(v, _dt.date):
            return _dt.datetime(v.year, v.month, v.day)
    s = str(v)[:10]
    import datetime as _dt
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            return _dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _parse_float_value(v):
    """解析浮点; 失败返回 None."""
    if v in (None, ""):
        return None
    try:
        f = float(v)
        return f if f == f else None  # NaN guard
    except (ValueError, TypeError):
        return None


_BA_CACHE = None

# D80 (2026-08-28): 导出时 Booking Agent 待确认清单 (匹配不到 / 多客商共用代码)
_BA_CONFIRM_ITEMS: List[Dict[str, str]] = []


def _record_ba_confirm(carrier: str, ba: str, reason: str) -> None:
    _BA_CONFIRM_ITEMS.append({"carrier": carrier, "booking_agent": ba, "reason": reason})


def get_ba_confirm_items() -> List[Dict[str, str]]:
    """导出后调用: 返回待业务确认的 Booking Agent 清单."""
    return list(_BA_CONFIRM_ITEMS)


def reset_ba_confirm_items() -> None:
    _BA_CONFIRM_ITEMS.clear()


_CW_PORTS_CACHE = None


def _load_cw_ports():
    """D79 (2026-08-27): 加载 CargoWare 平台港口表 (docs/cargoware平台所有海港.xls).

    CargoWare 用自己的港口代码体系 (如 ALGER/DZALG 并存, 苏丹港 SDPZU, 伊斯坦布尔 TRIST),
    与 ports.json 的 UN/LOCODE 不完全一致. 导出 POD 必须以 CargoWare 表为权威校验源.
    Returns: (code_to_en, en_to_code) 索引 (进程内缓存).
    """
    global _CW_PORTS_CACHE
    if _CW_PORTS_CACHE is not None:
        return _CW_PORTS_CACHE
    import os as _os
    candidates = [
        _os.path.join(_SCRIPT_DIR, "..", "..", "..", "docs", "cargoware平台所有海港.xls"),
        "/home/node/.openclaw/workspace/docs/cargoware平台所有海港.xls",
        _os.environ.get("DG_CW_PORTS_TABLE", ""),
    ]
    path = next((p for p in candidates if p and _os.path.exists(p)), None)
    if not path:
        _CW_PORTS_CACHE = ({}, {})
        return _CW_PORTS_CACHE
    try:
        import xlrd
        ws = xlrd.open_workbook(path).sheet_by_index(0)
        code_to_en = {}
        en_to_code = {}
        for r in range(1, ws.nrows):
            code = str(ws.cell_value(r, 0) or "").strip().upper()
            en = str(ws.cell_value(r, 2) or "").strip().upper()
            if not code:
                continue
            code_to_en.setdefault(code, en)
            if en and en not in en_to_code:
                en_to_code[en] = code
        _CW_PORTS_CACHE = (code_to_en, en_to_code)
    except Exception:
        _CW_PORTS_CACHE = ({}, {})
    return _CW_PORTS_CACHE


def _normalize_pod(pod, pod_name: str = "") -> Tuple[str, str]:
    """D79 (2026-08-27): POD 以 CargoWare 港口表为权威校验/纠正 — 修 "POD 不正确".

    代码在 CargoWare 表存在 → 保留 (CargoWare 用自己的代码, 如 ALGER/SDPZU/TRIST);
    不存在 → pod_name 英文名反查纠正 (VNHPH+HAIPHONG→SBHHG); 都无 → 保留 + 警告.
    Returns: (pod_code, warning)
    """
    p = str(pod or "").strip().upper()
    if not p:
        return "", ""
    code_to_en, en_to_code = _load_cw_ports()
    if p in code_to_en:
        return p, ""
    name = str(pod_name or "").strip().upper()
    if name and name in en_to_code:
        return en_to_code[name], f"POD {p}({name})→{en_to_code[name]} (纠正: 英文名匹配 CargoWare 代码)"
    # 已知错误码显式映射 (D79: 解析/历史数据产生的非 CargoWare 码)
    _KNOWN_FIX = {
        "TRALI": "TRIZA",  # 土耳其阿利亚加港 Aliaga → CargoWare TRIZA (IZMIR ALIAGA)
        "VNHPH": "SBHHG",  # 海防 → CargoWare SBHHG (HAIPHONG)
    }
    if p in _KNOWN_FIX:
        return _KNOWN_FIX[p], f"POD {p}→{_KNOWN_FIX[p]} (纠正: 已知错误码)"
    return p, f"POD {p} 不在 CargoWare 港口表, 需人工确认"


def _get_booking_agent(entry) -> str:
    """D80 (2026-08-28): 导出 Booking Agent = CargoWare 订舱代理代码.

    优先级:
      ① entry.booking_agent (飞书表, 订舱口中文名称) → 反查 CargoWare 代码
      ② 空/反查不到 → carrier → 订舱口正向匹配 → 中文名 → 反查代码 (兼容存量空数据)
      ③ 匹配不到 / 命中多客商共用代码 → 记入待确认清单, 返回空 (由业务确认)
    修复 CargoWare 导入报 "Booking Agent 不正确": 代码均来自 CargoWare 已录入客商表.
    """
    ba = (getattr(entry, "booking_agent", "") or "").strip()
    carrier = (getattr(entry, "carrier", "") or "").strip()
    try:
        from booking_agent_master import get_ba_master
        master = get_ba_master()
    except Exception:
        return ba

    def _name_to_code(name: str) -> str:
        code, st = master.resolve_code(name)
        if st == "ambiguous":
            _record_ba_confirm(carrier, name, "多客商共用该代码, 需业务确认")
            return ""
        if code:
            return code
        return None  # type: ignore[return-value]

    if ba:
        code = _name_to_code(ba)
        if code is not None:
            return code
    if carrier:
        name, st = master.resolve(carrier)
        if st == "ok" and name:
            code = _name_to_code(name)
            if code is not None:
                return code
        elif st == "ambiguous":
            _record_ba_confirm(carrier, ba, "多客商共用该代码, 需业务确认")
            return ""
    _record_ba_confirm(carrier, ba, "未匹配到 CargoWare 订舱代理代码")
    return ""


def _derive_rod(entries) -> str:
    """D79 (2026-08-27): 从 entries 推导目的区域(ROD) 填模板第 3 行 B 列.

    之前 write_xls 不传 meta_rod → 模板 ROD 恒空 → CargoWare 导入报错.
    取第一条非空 rod (enrich_regions_dict 按 pod 推导, 已有值保留).
    """
    try:
        from rate_io import enrich_regions_dict
    except ImportError:
        enrich_regions_dict = None
    for e in entries:
        rod = getattr(e, "rod", "") or ""
        if not rod and enrich_regions_dict:
            d = enrich_regions_dict({"pol": getattr(e, "pol", ""), "pod": getattr(e, "pod", ""), "rod": ""})
            rod = d.get("rod", "") or ""
        if rod:
            return str(rod).strip()
    return ""


def write_xls(rows: List[Dict[str, str]], output_path: str,
               meta_rod: str = "", meta_vat_cost: str = "0.0", meta_vat_sell: str = "0.0",
               template_path: str = None):
    """写真正的 .xls 文件（56 列全表，与 CargoWare 模板一致）。

    v3.10.6 (P0-B + P0-C 修复):
    - 优先使用 xlutils.copy 从 FCL_sample(20260506).xls 复制模板, 保留红字/黄背景/边框/字体
    - 按列类型分写: NUM_COLUMNS→float, DATE_COLUMNS→datetime (yyyy-mm-dd), 其余 str
    - 模板不可用或 xlutils 缺失时, 回退到 legacy 实现 (空白工作簿, 全 string)
    """
    import os as _os
    tpl = template_path or _os.environ.get("DG_CW_TEMPLATE") or DEFAULT_TEMPLATE_PATH
    if tpl and _os.path.exists(tpl):
        try:
            return _write_xls_with_template(rows, output_path, tpl, meta_rod, meta_vat_cost, meta_vat_sell)
        except ImportError as e:
            print("[export_cw] template-based export skipped (missing deps: " + str(e) + "); falling back to legacy", file=sys.stderr, flush=True)
        except Exception as e:
            print("[export_cw] template-based export failed: " + str(e) + "; falling back to legacy", file=sys.stderr, flush=True)
    return _write_xls_legacy(rows, output_path, meta_rod, meta_vat_cost, meta_vat_sell)


def _write_xls_legacy(rows: List[Dict[str, str]], output_path: str,
                      meta_rod: str, meta_vat_cost: str, meta_vat_sell: str) -> bool:
    """legacy 实现: 全 string 写空白工作簿 (P0-B/P0-C 未修复, 仅作 fallback)."""
    try:
        import xlwt
    except ImportError:
        print("[ERROR] xlwt 未安装，请运行 pip install xlwt", file=sys.stderr, flush=True)
        raise RuntimeError("xlwt 未安装, 无法生成 .xls. 请运行: pip install xlwt")
    wb = xlwt.Workbook(encoding="utf-8")
    sh = wb.add_sheet("Sheet1")
    # 样式
    header_style = xlwt.easyxf("font: bold on; align: horiz left; borders: left thin, right thin, top thin, bottom thin")
    meta_style = xlwt.easyxf("font: bold on;")
    cell_style = xlwt.easyxf("align: horiz left; borders: left thin, right thin, top thin, bottom thin")
    yellow_required = xlwt.easyxf("pattern: pattern solid, fore_colour yellow; align: horiz left; borders: left thin, right thin, top thin, bottom thin")
    red_required = xlwt.easyxf("font: colour red; align: horiz left; borders: left thin, right thin, top thin, bottom thin")
    # 元数据 6 行
    meta_rows = [list(META_ROW_1), list(META_ROW_2), list(META_ROW_3),
                 list(META_ROW_4), list(META_ROW_5), list(META_ROW_6)]
    if meta_rod:
        meta_rows[2][1] = meta_rod
    if meta_vat_cost:
        meta_rows[4][1] = meta_vat_cost
    if meta_vat_sell:
        meta_rows[5][1] = meta_vat_sell
    for i, mrow in enumerate(meta_rows):
        for j, v in enumerate(mrow):
            sh.write(i, j, v, meta_style)
    # 列头（第 7 行 = index 6）
    for j, col in enumerate(CW_COLUMNS):
        # 红色字体列：POL/POD/柜型/费率/Carrier/Booking Agent/Valid fm/to
        red_cols = {"POL", "POD", "20'", "40'", "40'HC", "20'NOR", "40'NOR", "45'",
                    "Carrier", "Booking Agent", "Valid fm", "Valid to", "P/C"}
        yellow_cols = set()  # 模板说"黄色必填"，但具体哪些列在 xls 里没显式
        if col in red_cols:
            sh.write(6, j, col, red_required)
        elif col in yellow_cols:
            sh.write(6, j, col, yellow_required)
        else:
            sh.write(6, j, col, header_style)
    # 数据行
    for ri, row in enumerate(rows):
        for j, col in enumerate(CW_COLUMNS):
            v = row.get(col, "")
            sh.write(7 + ri, j, v, cell_style)
    # 列宽
    sh.col(0).width = 256 * 8   # POL
    sh.col(1).width = 256 * 8   # POD
    sh.col(2).width = 256 * 8   # VIA
    sh.col(19).width = 256 * 10  # Carrier
    sh.col(23).width = 256 * 18  # Booking Agent
    sh.col(43).width = 256 * 60  # Remark（要长）
    wb.save(output_path)
    print("已写入 " + str(len(rows)) + " 条到 " + output_path)
    return True


def _write_xls_with_template(rows: List[Dict[str, str]], output_path: str, template_path: str,
                              meta_rod: str, meta_vat_cost: str, meta_vat_sell: str) -> bool:
    """v3.10.6: 用 xlutils.copy 从模板复制工作簿, 保留所有格式; 按列类型写值.

    Raises ImportError if xlrd/xlwt/xlutils not installed.
    Raises other exceptions for the caller to decide on fallback.
    """
    import xlwt  # noqa: F401  (may be used by callers; force availability check)
    import xlrd
    from xlutils.copy import copy as _xlu_copy

    rb = xlrd.open_workbook(template_path, formatting_info=True)
    wb = _xlu_copy(rb)
    sh = wb.get_sheet(0)
    n_data = len(rows)

    # Overwrite metadata cells (rows 0-5) with optional overrides
    meta_overrides = [(2, 1, meta_rod), (4, 1, meta_vat_cost), (5, 1, meta_vat_sell)]
    for (r, c, v) in meta_overrides:
        if v:
            sh.write(r, c, str(v))

    # Date style with yyyy-mm-dd format
    date_style = xlwt.XFStyle()
    date_style.num_format_str = "yyyy-mm-dd"

    # Determine data rows to clear (row 7 onwards): template may have sample data
    tpl_nrows = rb.sheet_by_index(0).nrows
    for r in range(7, tpl_nrows):
        for c in range(56):
            sh.write(r, c, "")

    # Write actual data rows (preserving per-cell xf_index from template)
    for ri, row in enumerate(rows):
        target_row = 7 + ri
        for j, col in enumerate(CW_COLUMNS):
            v = row.get(col, "")
            if v in (None, ""):
                continue
            if col in NUM_COLUMNS:
                fv = _parse_float_value(v)
                if fv is not None:
                    sh.write(target_row, j, fv)
                else:
                    sh.write(target_row, j, str(v))
            elif col in DATE_COLUMNS:
                dv = _parse_date_value(v)
                if dv is not None:
                    sh.write(target_row, j, dv, date_style)
                else:
                    sh.write(target_row, j, str(v))
            else:
                sh.write(target_row, j, str(v))

    # 列宽 (沿用 legacy 数值, 仅设置已知列避免覆盖模板列宽)
    col_widths = {0: 8, 1: 8, 2: 8, 19: 10, 23: 18, 43: 60}
    for col_idx, char_w in col_widths.items():
        try:
            sh.col(col_idx).width = 256 * char_w
        except Exception:
            pass

    wb.save(output_path)
    print("[export_cw] v3.10.6 template-based: " + str(n_data) + " 条写到 " + output_path + " (tpl=" + template_path + ")")
    return True


# ============================================================
# 兼容旧 API：导出 CSV
# ============================================================
def export_to_csv(entries, output_path: str, include_dg_only: bool = False):
    """兼容旧 API：写 CSV（22 列简版）。"""
    import csv
    if include_dg_only:
        entries = [e for e in entries if e.dg_surcharges]
    csv_cols = [
        "POL", "POD", "VIA", "DIRECT", "T/T", "FREQUENCY",
        "CARRIER", "BOOKING AGENT",
        "20'", "40'", "40'HC", "20'NOR", "40'NOR", "45'",
        "VALID FM", "VALID TO", "REMARK",
        "CONTRACT NO", "ENS", "AMS", "OVERLOAD REMARK", "FREE TIME",
    ]
    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=csv_cols, restval="")
        # 元数据
        today = datetime.date.today().strftime("%Y-%m-%d")
        f.write("\t".join(["FCL3.1 RATE TEMPLATE", "EXPORTED BY DG-LOGISTICS-AGENT", "", "", "", ""]) + "\n")
        f.write("\t".join(["Rate Type:", "FAK", "", "", "", ""]) + "\n")
        f.write("\t".join(["Currency:", "USD", "", "", "", ""]) + "\n")
        f.write("\t".join(["Origin:", "ALL", "", "", "", ""]) + "\n")
        f.write("\t".join(["Destination:", "ALL", "", "", "", ""]) + "\n")
        f.write("\t".join(["Effective Date:", today, "", "", "", ""]) + "\n")
        writer.writeheader()
        for e in entries:
            row = {
                "POL": e.pol or "", "POD": e.pod or "",
                "VIA": e.via_port or "", "DIRECT": e.direct or "",
                "T/T": str(e.tt_days) if e.tt_days else "",
                "FREQUENCY": e.frequency or "",
                "CARRIER": e.carrier or "", "BOOKING AGENT": e.booking_agent or "",
                "20'": _fmt_price(e.of_20), "40'": _fmt_price(e.of_40),
                "40'HC": _fmt_price(e.of_40hq), "20'NOR": _fmt_price(e.of_20nor),
                "40'NOR": _fmt_price(e.of_40nor), "45'": _fmt_price(e.of_45),
                "VALID FM": e.valid_from or "", "VALID TO": e.valid_to or "",
                "REMARK": format_dg_remark(e),
                "CONTRACT NO": e.contract_no or "",
                "ENS": _fmt_int(e.ens) if e.ens else "",
                "AMS": _fmt_int(e.ams) if e.ams else "",
                "OVERLOAD REMARK": e.ows_note or "",
                "FREE TIME": str(e.free_time) if e.free_time else "",
            }
            writer.writerow(row)
    print("已导出 " + str(len(entries)) + " 条到 " + output_path + " (CSV 简版)")


# ============================================================
# 空白模板
# ============================================================
def export_template(output_path: str):
    """导出空白模板：1 行示例数据 + 完整 56 列表头。"""
    sample = NormalizedRateEntry()
    sample.pol = "CNSHA"
    sample.pod = "THBKK"
    sample.carrier = "SITC"
    sample.of_20 = 350
    sample.of_40 = 650
    sample.of_40hq = 700
    sample.valid_from = "2026-07-01"
    sample.valid_to = "2026-07-31"
    sample.tt_days = 7
    sample.direct = "Y"
    sample.frequency = "WED/FRI"
    sample.remark = "示例数据（请删除）"
    row = build_row(sample)
    write_xls([row], output_path)


# ============================================================
# 主入口
# ============================================================
def _preview_value(value):
    """Render an exact source value for dry-run; never fill missing data."""
    if value is None:
        return "—"
    rendered = str(value).strip()
    return rendered if rendered else "—"




# ---------- v3.7: 飞书云盘上传 + 拿分享链接 ----------

def upload_to_drive(local_path: str, name: str = None) -> Dict[str, Any]:
    """上传 .xls 到飞书云盘，返回 {file_token, url, file_name, size}.

    v3.7 决策 3: 导出 .xls 后不再发本地路径, 而是上传云盘拿 URL, 由可可发链接给业务人员.

    重要 (2026-08-01 Q16 修复): 返回的 `url` 是 **飞书云盘预览链接** (HTML wrapper),
    不是 raw .xls 二进制. **不要用 `curl <url>` 验证 .xls 内容** (会拿到 HTML).
    正确验证方式: 用 `lark-cli --as user drive +download --file-token <file_token> --output ./local.xls`
    然后用 `xlrd` 等工具读 .xls. file_token 是稳定标识, URL 是会变的预览链接.
    """
    if not os.path.exists(local_path):
        return {"ok": False, "error": f"local file not found: {local_path}"}

    work_dir = os.path.dirname(os.path.abspath(local_path))
    file_basename = os.path.basename(local_path)
    upload_name = name or file_basename

    try:
        result = subprocess.run(
            [
                "lark-cli", "--as", "user",
                "drive", "+upload",
                "--file", f"./{file_basename}",
                "--name", upload_name,
                "--format", "json",
            ],
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            return {"ok": False, "error": f"lark-cli exit {result.returncode}: {result.stderr[:500]}"}
        out = json.loads(result.stdout)
        if not out.get("ok"):
            return {"ok": False, "error": out.get("error", {}).get("message", "unknown")}
        data = out.get("data", {})
        return {
            "ok": True,
            "file_token": data.get("file_token"),
            "url": data.get("url"),
            "file_name": data.get("file_name"),
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "lark-cli timeout (120s)"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:500]}


def send_drive_link(chat_id: str, url: str, desc: str, at_user_open_id: str = "") -> Dict[str, Any]:
    """发飞书消息 (post 类型) 给业务人员, 含云盘链接和 @.

    v3.7 决策 3: 唯一允许形式 = post 消息 + URL 链接.
    """
    if not chat_id or not url:
        return {"ok": False, "error": "chat_id 和 url 必填"}

    content_blocks = []
    if at_user_open_id:
        content_blocks.append({"tag": "at", "user_id": at_user_open_id})
        content_blocks.append({"tag": "text", "text": " "})
    content_blocks.append({"tag": "text", "text": desc + "\n"})
    content_blocks.append({"tag": "a", "text": "Cargoware 模板下载", "href": url})

    post = {"zh_cn": {"title": "运价库 Cargoware 模板导出", "content": [content_blocks]}}

    try:
        result = subprocess.run(
            [
                "lark-cli", "--as", "user",
                "im", "+messages-send",
                "--chat-id", chat_id,
                "--msg-type", "post",
                "--content", json.dumps(post, ensure_ascii=False),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            return {"ok": False, "error": f"lark-cli exit {result.returncode}: {result.stderr[:500]}"}
        out = json.loads(result.stdout)
        if not out.get("ok"):
            return {"ok": False, "error": out.get("error", {}).get("message", "unknown")}
        return {"ok": True, "message_id": out.get("data", {}).get("message_id")}
    except Exception as e:
        return {"ok": False, "error": str(e)[:500]}


def _export_postprocess(args, rows) -> None:
    """v3.7: 导出 .xls 后处理 - 上传飞书云盘 + (可选) 发消息给业务人员."""
    # D80 (2026-08-28): 输出 Booking Agent 待确认清单 (无论上传与否)
    confirm_items = get_ba_confirm_items()
    if confirm_items:
        print("=" * 60)
        print(f"[D80] ⚠️ {len(confirm_items)} 条记录 Booking Agent 需人工确认 (未填代码):")
        for item in confirm_items:
            print(f"  carrier={item['carrier']!r} ba={item['booking_agent']!r} 原因: {item['reason']}")
        print("  请业务确认后补充/修正订舱代理, 再重新导出.")
        print("=" * 60)
    reset_ba_confirm_items()
    is_xls = args.output.endswith(".xls")
    if not rows:
        print("[ERROR] 没有可导出的记录，未上传空模板。", file=sys.stderr)
        return
    if args.no_upload or not is_xls:
        print(f"[INFO] 跳过云盘上传 (--no_upload={args.no_upload}, is_xls={is_xls}). 输出: {args.output}")
        return

    print(f"[UPLOAD] 正在上传 {args.output} 到飞书云盘...")
    upload_result = upload_to_drive(args.output)
    if not upload_result.get("ok"):
        print(f"[ERROR] 云盘上传失败: {upload_result.get('error')}")
        print(f"[HINT] 本地路径仍可用: {args.output}")
        return

    url = upload_result.get("url", "")
    file_name = upload_result.get("file_name", "")
    file_token = upload_result.get("file_token", "")
    print(f"[UPLOAD] 上传成功: {file_name} (file_token={file_token})")
    print(f"[URL] {url}")
    print(f"[HINT] url 是飞书云盘预览链接 (HTML wrapper). 真实 .xls 下载用: lark-cli --as user drive +download --file-token {file_token} --output ./local.xls")

    result_json = {
        "ok": True,
        "code": "ok",
        "local_path": args.output,
        "file_token": upload_result.get("file_token"),
        "url": url,
        "file_name": file_name,
        "row_count": len(rows) if rows else 0,
    }
    print("[RESULT_JSON_BEGIN]")
    print(json.dumps(result_json, ensure_ascii=False, indent=2))
    print("[RESULT_JSON_END]")

    if args.send_to:
        carrier_count = len(rows) if rows else 0
        desc = f"Cargoware 模板已生成 ({carrier_count} 条运价)"
        send_result = send_drive_link(args.send_to, url, desc, args.at_user)
        if send_result.get("ok"):
            print(f"[SEND] 消息已发到 {args.send_to} (message_id={send_result.get('message_id')})")
        else:
            print(f"[ERROR] 发消息失败: {send_result.get('error')}")


def main():
    ap = argparse.ArgumentParser(description="CargoWare FCL3.1 导出工具")
    ap.add_argument("input", nargs="?", help="解析结果 JSON 文件路径")
    ap.add_argument("-o", "--output", default="", help="输出 .xls 路径（默认: cargoware_export_YYYYMMDD_HHMMSS.xls）")
    ap.add_argument("--dg-only", action="store_true", help="仅导出含 DG 附加费的运价")
    ap.add_argument("--template", action="store_true", help="导出空白模板（1 行示例）")
    ap.add_argument("--from-feishu", action="store_true", help="从飞书多维表格直接拉取（覆盖 input）")
    ap.add_argument("--csv", action="store_true", help="输出 CSV 简版（22 列）")
    ap.add_argument("--status", default="", choices=["", "待补充", "已生效"], help="从飞书拉取时的状态过滤（默认不按状态过滤）")
    ap.add_argument("--row-range", default="", help="按运价编号范围筛选，格式 \"3203-3204\" 或 \"NO.3203-NO.3204\" (含两端)")
    ap.add_argument("--rate-no", default="", help="按运价编号筛选，多个用逗号分隔 (如 NO.5558,NO.5559)")
    ap.add_argument("--carrier", default="", help="按船公司筛选")
    ap.add_argument("--pol", default="", help="按起运港筛选")
    ap.add_argument("--pod", default="", help="按目的港筛选")
    ap.add_argument("--import-after", default="", help="按导入时间筛选 (格式: YYYY-MM-DD, 导入时间 >= 此值)")
    ap.add_argument("--import-before", default="", help="按导入时间筛选 (格式: YYYY-MM-DD, 导入时间 <= 此值)")
    ap.add_argument("--booking-agent", default="", help="按订舱代理筛选 (D93: 归一化精确匹配; 短名如\"中外运\"/长法人名如\"中外运集装箱运输有限公司\"均直接可查, 不子串模糊)")
    ap.add_argument("--dry-run", action="store_true", help="仅打印要导出的记录列表与关键字段，不生成文件")
    ap.add_argument("--no-upload", action="store_true", help="不上传飞书云盘 (默认上传, v3.7 决策 3)")
    ap.add_argument("--send-to", default="", help="导出后发链接到指定 chat_id (1v1 或群, 可选)")
    ap.add_argument("--at-user", default="", help="--send-to 时 @ 的飞书用户 open_id (可选)")
    args = ap.parse_args()
    if not args.output:
        # 默认文件名加日期时间戳 (Asia/Shanghai)
        ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        args.output = f"cargoware_export_{ts}.xls"

    # 模板
    if args.template:
        export_template(args.output)
        return _export_postprocess(args, [])

    # 从飞书拉
    if args.from_feishu:
        from feishu_source import fetch_rates_from_feishu
        # 解析 --row-range "10-25" -> (10, 25)
        row_range = None
        if args.row_range:
            parts = args.row_range.replace("NO.", "").split("-")
            assert len(parts) == 2, f"--row-range 格式应为 start-end, 收到 {args.row_range}"
            row_range = (int(parts[0].strip()), int(parts[1].strip()))
            assert row_range[0] >= 1 and row_range[1] >= row_range[0], f"--row-range 无效: {row_range}"
        entries, dg_by_link = fetch_rates_from_feishu(
            status_filter=args.status,
            include_dg=True,
            only_valid=False,  # 不按有效期过滤 (业务规则: 全部运价都可导出, 由用户筛选条件决定)
            row_range=row_range,
            rate_no_filter=args.rate_no,
            carrier_filter=args.carrier,
            pol_filter=args.pol,
            pod_filter=args.pod,
            import_after=args.import_after,
            import_before=args.import_before,
            booking_agent_filter=args.booking_agent,
        )
        print("从飞书拉取 %d 条运价（不限有效期/状态，按筛选条件过滤）" % len(entries))
        if not entries:
            print("[ERROR] 未找到符合条件的运价记录，未生成导出文件。", file=sys.stderr)
            return 2
        # dry-run: 仅打印飞书真实字段, 不生成文件；禁止 LLM 依据历史上下文补值
        if args.dry_run:
            print(f"[DRY-RUN][SOURCE=FEISHU][NO-INFERENCE] 将导出 {len(entries)} 条运价:")
            print("[SCHEMA_AUTHORITATIVE] 20GP=20GP O/F(USD); 40GP=40GP O/F(USD); 40HQ=40HQ O/F(USD)")
            for e in entries:
                rid = getattr(e, "_record_id", "")
                rno = getattr(e, "_row_no", "?")
                values = {
                    "20GP": _preview_value(getattr(e, "of_20", None)),
                    "40GP": _preview_value(getattr(e, "of_40", None)),
                    "40HQ": _preview_value(getattr(e, "of_40hq", None)),
                    "20NOR": _preview_value(getattr(e, "of_20nor", None)),
                    "40NOR": _preview_value(getattr(e, "of_40nor", None)),
                    "45": _preview_value(getattr(e, "of_45", None)),
                }
                print(
                    "  [row={:>3}] rec={} POL={} POD={} carrier={} ba={} pc={} "
                    "20GP={} 40GP={} 40HQ={} 20NOR={} 40NOR={} 45={} "
                    "valid={}~{} status={}".format(
                        rno,
                        rid,
                        _preview_value(getattr(e, "pol", None)),
                        _preview_value(getattr(e, "pod", None)),
                        _preview_value(getattr(e, "carrier", None)),
                        _preview_value(getattr(e, "booking_agent", None)),
                        _preview_value(getattr(e, "pc", None)),
                        values["20GP"], values["40GP"], values["40HQ"],
                        values["20NOR"], values["40NOR"], values["45"],
                        _preview_value(getattr(e, "valid_from", None)),
                        _preview_value(getattr(e, "valid_to", None)),
                        _preview_value(getattr(e, "status", None)),
                    )
                )
                empty_prices = [name for name, value in values.items() if value == "—"]
                if empty_prices:
                    print("      [EMPTY_SOURCE_FIELDS] " + ",".join(empty_prices))
            print("[DRY-RUN] 以上值全部来自本次飞书查询；— 表示源字段为空，不得补写.")
            print("[DRY-RUN] 跳过文件生成. 如确认请去掉 --dry-run 重跑.")
            return
        if args.dg_only:
            entries = [e for e in entries if e.dg_surcharges or dg_by_link.get(getattr(e, "_record_id", ""))]
        rows = []
        for e in entries:
            rec_id = getattr(e, "_record_id", "")
            extra_dg = dg_by_link.get(rec_id, [])
            rows.append(build_row(e, extra_dg))
        if args.csv:
            export_to_csv(entries, args.output, include_dg_only=False)
        else:
            write_xls(rows, args.output, meta_rod=_derive_rod(entries))
        return _export_postprocess(args, rows)

    # 从 JSON 解析
    if not args.input:
        print("[ERROR] 请提供输入 JSON 路径 或 用 --from-feishu / --template", file=sys.stderr)
        sys.exit(1)
    if not os.path.exists(args.input):
        print("[ERROR] 文件不存在: " + args.input, file=sys.stderr)
        sys.exit(1)
    with open(args.input, "r", encoding="utf-8-sig") as f:
        data = json.load(f)
    entries = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and "entries" in item:
                entries.extend([NormalizedRateEntry.from_dict(e) for e in item["entries"]])
            elif isinstance(item, dict):
                entries.append(NormalizedRateEntry.from_dict(item))
    elif isinstance(data, dict) and "entries" in data:
        entries = [NormalizedRateEntry.from_dict(e) for e in data["entries"]]
    if not entries:
        print("[WARN] 未找到运价记录", file=sys.stderr)
        sys.exit(1)
    if args.csv:
        export_to_csv(entries, args.output, include_dg_only=args.dg_only)
        return _export_postprocess(args, [])
    if args.dg_only:
        entries = [e for e in entries if e.dg_surcharges]
    rows = [build_row(e) for e in entries]
    write_xls(rows, args.output, meta_rod=_derive_rod(entries))
    return _export_postprocess(args, rows)


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr, flush=True)
        sys.exit(2)