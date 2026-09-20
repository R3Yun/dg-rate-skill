# -*- coding: utf-8 -*-
"""
从飞书多维表格拉取运价数据

拉取 FCL海运费表 中的运价, DG附加费已合入 FCL 表备注字段 (v3.1 决策, 2026-07-14).
- 默认只拉 状态=已生效 且 在有效期内的运价
- DG 附加费不单独建表 (v3.1 决策, 2026-07-15 已删飞书表 tblY2xJgMvYEtxkO)

底层命令：lark-cli base +field-list / +record-list / +record-search
通过 paramiko SSH 到 NAS 调用容器内 lark-cli。

关键设计 (2026-07-10 重新梳理)：
1. +field-list 返回 {id, name, type}，字段键是 id/name (不是 field_id/field_name)
2. +record-list 返回 data.data 是 2D 位置数组；
   列顺序对应 data.field_id_list；
   行顺序对应 data.record_id_list；
   这两个数组必须 zip 起来还原 record_id + fields 结构。
3. +record-list 不支持 --page-token，用 --offset/--limit 分页；
   has_more=true 时继续拉下一页。
4. --field-id 多次传入可投影指定列；建议用 field ID（不用中文名）避免 shell 编码问题。
"""

import json
import os
import sys
import datetime
import time
from typing import Dict, Any, List, Optional, Tuple

try:
    import paramiko
    HAS_PARAMIKO = True
except ImportError:
    HAS_PARAMIKO = False

from rate_io import NormalizedRateEntry, DGSurcharge


DEFAULT_CONFIG = {
    "nas_host": "192.168.31.128",
    "nas_port": int(os.environ.get("NAS_SSH_PORT", "2122")),
    "nas_user": "admin",
    "nas_password": "Zengs_19761029",
    "container_name": "Openclaw-coco",
    "base_token": "Eje8bWtVdaPPPosu0GQcPclQnut",
    "rate_table_id": "tblnCWVGvCfFHW6m",
    # v3.7+: 导入记录表 tblmindpCoSaIEsY 已删除
    "partner_table_id": "tbl6KdkpgylccjT7",
}


# ---------- 字段名常量 (与 FCL海运费表 对齐; DG附加费已合入 FCL 备注字段 v3.1) ----------

RATE_FIELD = {
    "POL": "fldKtetOx2",
    "POD": "fldO48DLjK",
    "VIA中转港": "fldqqJyVdI",
    "船公司": "fldh2VRr8A",
    "订舱代理": "fld98P0EV5",
    "20GP O/F(USD)": "fldtxsWEd2",
    "40GP O/F(USD)": "fld4NZ5Hjf",
    "40HQ O/F(USD)": "fldt34a32J",
    "20NOR O/F(USD)": "fldlshzjYR",
    "40NOR O/F(USD)": "fldueBBbKo",
    "45尺 O/F(USD)": "fldiQnMTug",
    # Q2 (2026-07-21): DG附加费 (Q2方案A: 3 个 number 字段, 与 O/F 对称)
    "20GP DG(USD)": "fldpOkjHyY",
    "40GP DG(USD)": "fldUYMl5Tk",
    "40HQ DG(USD)": "fldM5sfUJa",
    "航程(天)": "fld1UUm2YY",
    "班期": "fldZKqcjes",
    "直航": "fldWHV2wHt",
    "免柜期(天)": "fldXgWrkbW",
    "有效期起": "fldCZtkrvy",
    "有效期止": "fld0O5Wobz",
    "合约号": "fldNek9BEV",
    "P/C": "fldbTLNcdA",
    "备注": "fldxZveF4Z",
    "运价编号": "flds8EwiM2",
    # "来源文件" 字段已删 (2026-07-16 v3.2 字段瘦身; 文件类型已并入"备注"或"原文件附件"字段)
    "解析置信度": "fldfMHmQem",
    "状态": "fld5NqEqrn",
    "数据来源": "fldthltP8N",
    "运价类型": "fldBgcznYZ",
    "起运区域": "fldawROIDw",
    "目的区域": "fldSQ3iDGE",
    "AMS费用": "fldFjupOqE",
    "ENS费用": "fldQPeXzFU",
    "超重备注": "fldyZwTZZQ",
    # v3.7+: 导入人(fldwz5NTKe)/审核人(fldzZsajVV) 已删除
}


# ---------- 工具 ----------

def _today_str() -> str:
    return datetime.date.today().strftime("%Y-%m-%d")


def _first_str(v) -> str:
    """select 字段返回 list[str]，取第一个；空值返回 ""。"""
    if v is None or v == "":
        return ""
    if isinstance(v, list):
        return str(v[0]) if v else ""
    return str(v)


def _now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _to_float(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except Exception:
        return None


def _to_int(v):
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except Exception:
        return None


def _date_to_iso(v) -> str:
    if v is None or v == "":
        return ""
    if isinstance(v, (int, float)):
        try:
            ts = v / 1000 if v > 1e10 else v
            return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        except Exception:
            return str(v)
    s = str(v)
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        return s
    if len(s) >= 10 and " " in s:
        return s[:10]
    return s


def _clean_remark(remark: str) -> str:
    if not remark:
        return ""
    parts = []
    for seg in remark.split("|"):
        seg = seg.strip()
        if not seg:
            continue
        if seg.startswith("\u26a0") or seg.startswith("\U0001f6ab"):
            continue
        parts.append(seg)
    return " | ".join(parts)


def _to_entry_from_feishu_record(rec: Dict[str, Any]) -> NormalizedRateEntry:
    """把飞书记录转成 NormalizedRateEntry。"""
    fields = rec.get("fields", {}) or {}
    e = NormalizedRateEntry()
    e.pol = str(fields.get("POL", "") or "")
    e.pod = str(fields.get("POD", "") or "")
    e.via_port = str(fields.get("VIA\u4e2d\u8f6c\u6e2f", "") or "")
    e.carrier = _first_str(fields.get("船公司"))
    e.rate_no = str(fields.get("运价编号", "") or "")
    e.booking_agent = str(fields.get("\u8ba2\u8231\u4ee3\u7406", "") or "")
    # WS-163: 补齐导出字段映射 (Bug 1 P/C + Bug 2 Vessel/Voyage/ETD/ETA + D73 全称)
    e.pc = _first_str(fields.get("P/C"))  # P/C 单选 select 字段, 必须 _first_str (D7 闸门: 不默认 Both)
    e.vessel = str(fields.get("船名", "") or "")
    e.voyage = str(fields.get("航次", "") or "")
    e.etd = _date_to_iso(fields.get("ETD"))
    e.eta = _date_to_iso(fields.get("ETA"))
    e.pol_name = str(fields.get("起运港全称", "") or "")
    e.pod_name = str(fields.get("目的港全称", "") or "")
    e.of_20 = _to_float(fields.get("20GP O/F(USD)"))
    e.of_40 = _to_float(fields.get("40GP O/F(USD)"))
    e.of_40hq = _to_float(fields.get("40HQ O/F(USD)"))
    e.of_20nor = _to_float(fields.get("20NOR O/F(USD)"))
    e.of_40nor = _to_float(fields.get("40NOR O/F(USD)"))
    e.of_45 = _to_float(fields.get("45\u5c3a O/F(USD)"))
    e.tt_days = _to_int(fields.get("\u822a\u7a0b(\u5929)"))
    e.frequency = _first_str(fields.get("班期"))
    e.direct = _first_str(fields.get("直航"))
    e.free_time = _to_int(fields.get("\u514d\u67dc\u671f(\u5929)"))
    e.status = _first_str(fields.get("\u72b6\u6001"))
    e.valid_from = _date_to_iso(fields.get("\u6709\u6548\u671f\u8d77"))
    e.valid_to = _date_to_iso(fields.get("\u6709\u6548\u671f\u6b62"))
    e.import_time = _date_to_iso(fields.get("\u5bfc\u5165\u65f6\u95f4"))  # D84: 映射导入时间 (import_after/before 筛选依赖)
    e.contract_no = str(fields.get("\u5408\u7ea6\u53f7", "") or "")
    raw_remark = str(fields.get("\u5907\u6ce8", "") or "")
    e.remark = _clean_remark(raw_remark)
    e.source_file = str(fields.get("\u6765\u6e90\u6587\u4ef6", "") or "")
    e.parser = "feishu_source"
    e.source_type = "feishu_bitable"
    try:
        e.confidence = float(fields.get("\u89e3\u6790\u7f6e\u4fe1\u5ea6", 0) or 0) / 100.0
    except Exception:
        e.confidence = 1.0
    e.parsed_at = _now_iso()
    e.raw_excerpt = "feishu_record_id=" + str(rec.get("record_id", ""))
    setattr(e, "_record_id", str(rec.get("record_id", "")))
    setattr(e, "_row_no", 0)  # 由 fetch_rates 循环时填充 (1-based)
    return e


# ---------- SSH 客户端 ----------

class FeishuRateSource:
    def __init__(self, config: Dict[str, Any] = None):
        self.config = {**DEFAULT_CONFIG, **(config or {})}
        if not HAS_PARAMIKO:
            raise ImportError("paramiko \u672a\u5b89\u88c5")
        # cache: {table_id: [{"id": ..., "name": ..., "type": ...}, ...]}
        self._field_cache: Dict[str, List[Dict[str, str]]] = {}

    def _connect(self):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            self.config["nas_host"],
            port=self.config.get("nas_port", 2122),
            username=self.config["nas_user"],
            password=self.config["nas_password"],
            timeout=30,
            look_for_keys=False,
            allow_agent=False,
        )
        return client

    def _exec(self, cmd: str, timeout: int = 60, max_retry: int = 3) -> str:
        """执行 SSH 命令；遇 429 自动指数退避重试。"""
        last_err = ""
        for attempt in range(max_retry):
            client = self._connect()
            try:
                _, stdout, stderr = client.exec_command(cmd, timeout=timeout)
                out = stdout.read().decode("utf-8", errors="replace").strip()
                err = stderr.read().decode("utf-8", errors="replace").strip()
                result = out if out else err
                if "429" in result or "Too Many Requests" in result:
                    last_err = result
                    wait = 5 * (attempt + 1)
                    sys.stderr.write(
                        "[WARN] \u9047\u5230 429 \u9650\u6d41\uff0c\u7b49 %ds \u540e\u91cd\u8bd5 (%d/%d)\n" % (wait, attempt + 1, max_retry)
                    )
                    time.sleep(wait)
                    continue
                return result
            finally:
                client.close()
        return last_err

    def _lark_cmd(self, subcmd: str, **kwargs) -> str:
        """构造 lark-cli 命令（参数 shlex.quote 处理含空格/中文的值）。"""
        import shlex
        parts = ["sudo", "docker", "exec", self.config["container_name"], "lark", "base", subcmd]
        parts.append("--base-token")
        parts.append(self.config["base_token"])
        for k, v in kwargs.items():
            if v is None or v == "":
                continue
            if isinstance(v, (list, tuple)):
                for it in v:
                    parts.append(k)
                    parts.append(str(it))
            else:
                parts.append(k)
                parts.append(str(v))
        parts.append("--format")
        parts.append("json")
        return " ".join(shlex.quote(p) for p in parts) + " 2>&1"

    # ---------- 字段 ----------

    def list_field_info(self, table_id: str, force: bool = False) -> List[Dict[str, str]]:
        """拉取字段信息 [{id, name, type}]，按 name 排序确保顺序一致。"""
        if not force and table_id in self._field_cache:
            return self._field_cache[table_id]
        cmd = self._lark_cmd("+field-list", **{"--table-id": table_id, "--limit": 200})
        out = self._exec(cmd, timeout=30)
        try:
            data = json.loads(out)
        except Exception:
            raise RuntimeError("field-list \u8fd4\u56de\u975e JSON: " + out[:300])
        if data.get("ok") is False:
            raise RuntimeError("field-list \u5931\u8d25: " + json.dumps(data, ensure_ascii=False)[:300])
        items = (data.get("data", {}) or {}).get("fields") or []
        info = []
        for f in items:
            info.append({
                "id": f.get("id", "") or "",
                "name": f.get("name", "") or "",
                "type": f.get("type", "") or "",
            })
        info.sort(key=lambda x: x["name"])
        self._field_cache[table_id] = info
        return info

    def list_field_names(self, table_id: str) -> List[str]:
        return [f["name"] for f in self.list_field_info(table_id)]

    def list_field_ids(self, table_id: str) -> List[str]:
        return [f["id"] for f in self.list_field_info(table_id)]

    # ---------- 记录 ----------

    def list_all_records(self, table_id: str,
                         page_size: int = 100,
                         filter_status: str = None) -> List[Dict[str, Any]]:
        """拉取一张表所有记录（自动翻页 + 位置数组转 [{record_id, fields}]）。

        filter_status: 可选按"状态" select 字段值过滤（避免无效记录干扰）。
        """
        field_info = self.list_field_info(table_id)
        if not field_info:
            return []
        field_ids = [f["id"] for f in field_info]
        field_name_by_id = {f["id"]: f["name"] for f in field_info}

        all_records: List[Dict[str, Any]] = []
        offset = 0
        page = 0
        while True:
            page += 1
            cmd = self._lark_cmd(
                "+record-list",
                **{
                    "--table-id": table_id,
                    "--limit": page_size,
                    "--offset": offset,
                    "--field-id": field_ids,
                }
            )
            try:
                out = self._exec(cmd, timeout=90)
            except Exception as e:
                sys.stderr.write("[WARN] record-list \u8d85\u65f6: %s\n" % e)
                break
            try:
                data = json.loads(out)
            except Exception:
                sys.stderr.write("[WARN] record-list \u975e JSON: %s\n" % out[:200])
                break
            if data.get("ok") is False:
                err = (data.get("error") or {}).get("message", "") or json.dumps(data.get("error") or {})
                if "permission" in err.lower() or "scope" in err.lower():
                    raise PermissionError("\u7f3a\u5c11\u6743\u9650: " + err)
                sys.stderr.write("[WARN] record-list \u5931\u8d25: %s\n" % err)
                break
            inner = data.get("data", {}) or {}
            raw_items = inner.get("data") or []
            field_id_list = inner.get("field_id_list") or []
            record_id_list = inner.get("record_id_list") or []
            if not isinstance(raw_items, list):
                sys.stderr.write("[WARN] record-list data.data \u4e0d\u662f list\n")
                break
            for i, row in enumerate(raw_items):
                if not isinstance(row, list):
                    continue
                rid = record_id_list[i] if i < len(record_id_list) else ""
                fields_dict: Dict[str, Any] = {}
                for j, val in enumerate(row):
                    fid = field_id_list[j] if j < len(field_id_list) else None
                    if fid is None:
                        continue
                    fname = field_name_by_id.get(fid, fid)
                    fields_dict[fname] = val
                rec = {"record_id": rid, "fields": fields_dict}
                if filter_status:
                    raw_status = fields_dict.get("\u72b6\u6001", "")
                    if isinstance(raw_status, list):
                        status_vals = [str(x) for x in raw_status]
                    else:
                        status_vals = [str(raw_status or "")]
                    if filter_status not in status_vals:
                        continue
                all_records.append(rec)
            has_more = bool(inner.get("has_more", False))
            offset += len(raw_items)
            sys.stderr.write(
                "[INFO] \u9875 %d: \u62ff\u5230 %d \u6761, has_more=%s, offset=%d\n"
                % (page, len(raw_items), has_more, offset)
            )
            if not has_more or len(raw_items) < page_size:
                break
            # 速率控制：避免连续大查询触发 429
            time.sleep(0.5)
        return all_records

    # ---------- 业务方法 ----------

    def fetch_rates(self, status_filter: str = "",
                    only_valid: bool = True,
                    today: str = None,
                    row_range = None) -> List[NormalizedRateEntry]:
        """拉取运价；row_range 按运价编号 NO.nnn 的数字部分筛选。"""
        today = today or _today_str()
        records = self.list_all_records(self.config["rate_table_id"], filter_status=status_filter)
        entries: List[NormalizedRateEntry] = []
        for idx, r in enumerate(records, 1):  # 1-based 行号
            fields = r.get("fields", {}) or {}
            if row_range:
                start, end = row_range
                rate_no = str(fields.get("运价编号", ""))
                try:
                    rate_no_num = int(rate_no.replace("NO.", "").strip())
                except (TypeError, ValueError):
                    rate_no_num = None
                if rate_no_num is None or not (start <= rate_no_num <= end):
                    continue
            if only_valid:
                vf = _date_to_iso(fields.get("\u6709\u6548\u671f\u8d77"))
                vt = _date_to_iso(fields.get("\u6709\u6548\u671f\u6b62"))
                if vf and vf > today:
                    continue
                if vt and vt < today:
                    continue
            e = _to_entry_from_feishu_record(r)
            setattr(e, "_row_no", idx)
            entries.append(e)
        return entries

    def fetch_dg_surcharges_by_link(self, rate_record_ids: List[str]) -> Dict[str, List[Dict[str, Any]]]:
        """已废弃 (v3.1 决策: DG附加费合入 FCL 表备注, 无独立关联表).
        保留作为 stub 返回空 dict 以兼容旧调用方.
        """
        return {}
        result: Dict[str, List[Dict[str, Any]]] = {}
        for r in records:
            fields = r.get("fields", {}) or {}
            link = fields.get("\u5173\u8054\u8fd0\u4ef7", None)
            linked_ids = []
            if isinstance(link, list):
                for it in link:
                    if isinstance(it, dict) and it.get("record_id"):
                        linked_ids.append(it["record_id"])
                    elif isinstance(it, str):
                        linked_ids.append(it)
            elif isinstance(link, str):
                linked_ids = [link]
            for rid in linked_ids:
                if rid in rate_record_ids:
                    result.setdefault(rid, []).append(r)
        return result


# ---------- 顶层入口 ----------

def _norm_ba(s) -> str:
    """订舱代理归一化: 去首尾空白 (D93, 短名/长法人名各自精确匹配)."""
    return str(s or "").strip()


def fetch_rates_from_feishu(status_filter: str = "",
                            include_dg: bool = True,
                            only_valid: bool = True,
                            row_range = None,
                            rate_no_filter: str = "",
                            carrier_filter: str = "",
                            pol_filter: str = "",
                            pod_filter: str = "",
                            import_after: str = "",
                            import_before: str = "",
                            booking_agent_filter: str = "") -> Tuple[List[NormalizedRateEntry], Dict[str, List[Dict[str, Any]]]]:
    src = FeishuRateSource()
    entries = src.fetch_rates(status_filter=status_filter, only_valid=only_valid, row_range=row_range)
    
    # Apply additional filters
    if entries and (rate_no_filter or carrier_filter or pol_filter or pod_filter or import_after or import_before or booking_agent_filter):
        ba_query = _norm_ba(booking_agent_filter)
        filtered_entries = []
        for e in entries:
            if rate_no_filter:
                rate_nos = [x.strip() for x in rate_no_filter.split(",") if x.strip()]
                if rate_nos and not any(rn in (getattr(e, "rate_no", "") or "") for rn in rate_nos):
                    continue
            if carrier_filter and carrier_filter.lower() not in (getattr(e, "carrier", "") or "").lower():
                continue
            if pol_filter and pol_filter.lower() not in (getattr(e, "pol", "") or "").lower():
                continue
            if pod_filter and pod_filter.lower() not in (getattr(e, "pod", "") or "").lower():
                continue
            if import_after or import_before:
                import_time = getattr(e, "import_time", "") or ""
                if import_time:
                    import_date = import_time[:10]
                    if import_after and import_date < import_after:
                        continue
                    if import_before and import_date > import_before:
                        continue
            # D93 (2026-09-02): 订舱代理 — 归一化精确匹配 (不子串, 避免短名/长法人名两批混淆)
            if ba_query and _norm_ba(getattr(e, "booking_agent", "")) != ba_query:
                continue
            filtered_entries.append(e)
        entries = filtered_entries
    
    if not include_dg or not entries:
        return entries, {}
    rate_ids = [getattr(e, "_record_id", "") for e in entries]
    rate_ids = [r for r in rate_ids if r]
    dg_by_id = src.fetch_dg_surcharges_by_link(rate_ids)
    return entries, dg_by_id


def _main():
    import argparse
    ap = argparse.ArgumentParser(description="\u4ece\u98de\u4e66\u62c9\u8fd0\u4ef7")
    ap.add_argument("--status", default="\u751f\u6548\u4e2d", help="\u72b6\u6001\u8fc7\u6ee4")
    ap.add_argument("--all", action="store_true", help="\u4e0d\u8fc7\u6ee4\u6709\u6548\u671f")
    ap.add_argument("--include-dg", action="store_true", default=True)
    ap.add_argument("--format", choices=["json", "count"], default="count")
    args = ap.parse_args()
    entries, dg = fetch_rates_from_feishu(
        status_filter=args.status,
        include_dg=args.include_dg,
        only_valid=not args.all,
    )
    if args.format == "count":
        print("rate_count:", len(entries))
        print("dg_linked_rate_count:", len(dg))
        for e in entries[:10]:
            d = e.to_dict()
            print(" ", d["pol"], "->", d["pod"], "carrier=", d["carrier"],
                  "of_20=", d["of_20"], "valid=", d["valid_from"], "~", d["valid_to"])
    else:
        out = {
            "rate_count": len(entries),
            "entries": [],
            "dg_by_rate_id": {
                k: [{"record_id": v.get("record_id"), "fields": v.get("fields")} for v in vs]
                for k, vs in dg.items()
            },
        }
        for e in entries:
            d = e.to_dict()
            d["_record_id"] = getattr(e, "_record_id", "")
            out["entries"].append(d)
        print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _main()