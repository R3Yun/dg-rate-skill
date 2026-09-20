# -*- coding: utf-8 -*-
"""筛选并查找 FCL 表记录 record_ids

v3.10.10: 支持多维度筛选，替代单一 rate_no 查询。
- 按运价编号 (--rate-no, 支持逗号分隔多个)
- 按船公司 (--carrier, 模糊匹配)
- 按港口 (--pod/pol, 模糊匹配)
- 按导入时间范围 (--import-after, --import-before)
- 按订舱代理 (--booking-agent, 归一化精确匹配 — D93, WS-162 五轮打回)
  * 匹配语义: 输入值与库中「订舱代理」字段值去首尾空白后精确相等。
    短名(如"中外运")与长法人名(如"中外运集装箱运输有限公司")作为两种
    存储形态均可直接查询; 不做子串模糊(避免把两批记录混在一起)。
- 按有效期窗口 (--valid-on / --valid-after / --valid-before, D93)
  * --valid-on <date>: 记录在该日期有效 (有效期起 <= date <= 有效期止)
  * --valid-after <date>: 有效期止 >= date (该日期后仍有效)
  * --valid-before <date>: 有效期起 <= date (该日期前已生效)
- 组合筛选：所有条件取交集

D93 (2026-09-02, WS-162 五轮打回): 输出每条 record_id + 关键字段
(pol/pod/carrier/pc/valid_from/valid_to/booking_agent), 让 agent/业务员
能逐条核对定位结果 (如 苏比克 recvtFK50qq6Li PHSFS vs recvtGz18F5tBo SUBIC)。
"""
import argparse
import datetime
import json
import subprocess
import sys


def _norm(s) -> str:
    """归一化: 去首尾空白 (短名/长法人名各自精确匹配的基础)."""
    return str(s or "").strip()


def _date_to_iso(v) -> str:
    """把飞书日期值归一成 YYYY-MM-DD 字符串 (兼容毫秒时间戳/ISO/已格式化)."""
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
    if len(s) == 10 and s[4] == "/" and s[7] == "/":
        return s.replace("/", "-")
    return s


def list_all_records(base_token, table_id):
    """拉取表所有记录（分页），转成 [{record_id, fields}, ...] 格式"""
    all_records = []
    offset = 0
    page_size = 200
    while True:
        cmd = [
            "lark-cli", "--as", "user", "base", "+record-list",
            "--base-token", base_token,
            "--table-id", table_id,
            "--format", "json",
            "--page-size", str(page_size),
            "--offset", str(offset),
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if result.returncode != 0:
                return None, f"拉取失败: {result.stderr[:500]}"
            data = json.loads(result.stdout)
            if not data.get("ok"):
                return None, f"API 错误: {data.get('error', {})}"
            inner = data.get("data", {})
            items = inner.get("data", []) or []
            field_names = inner.get("fields", []) or []
            record_id_list = inner.get("record_id_list", []) or []
            for i, vals in enumerate(items):
                rid = record_id_list[i] if i < len(record_id_list) else ""
                fields_dict = {}
                if isinstance(vals, list):
                    for j, fname in enumerate(field_names):
                        if j < len(vals):
                            fields_dict[fname] = vals[j]
                else:
                    fields_dict = vals
                all_records.append({"record_id": rid, "fields": fields_dict})
            if not inner.get("has_more", False):
                break
            offset += len(items)
        except subprocess.TimeoutExpired:
            return None, "拉取超时"
        except Exception as e:
            return None, f"拉取异常: {str(e)}"
    return all_records, None


def apply_filters(records, rate_no="", carrier="", pod="", pol="",
                  import_after="", import_before="",
                  booking_agent="", valid_on="", valid_after="", valid_before=""):
    """所有条件取交集。booking_agent 归一化精确匹配; 有效期窗口见模块 docstring."""
    if not any([rate_no, carrier, pod, pol, import_after, import_before,
                booking_agent, valid_on, valid_after, valid_before]):
        return records
    rate_nos = [x.strip() for x in rate_no.split(",") if x.strip()] if rate_no else []
    ba_query = _norm(booking_agent) if booking_agent else ""
    filtered = []
    for r in records:
        fields = r.get("fields", {}) or {}
        if rate_nos:
            rn = str(fields.get("运价编号", "") or "")
            if not any(target in rn for target in rate_nos):
                continue
        if carrier:
            c = str(fields.get("船公司", "") or "")
            if carrier.lower() not in c.lower():
                continue
        if pod:
            p = str(fields.get("POD", "") or "")
            if pod.lower() not in p.lower():
                continue
        if pol:
            pl = str(fields.get("POL", "") or "")
            if pol.lower() not in pl.lower():
                continue
        if import_after or import_before:
            it = str(fields.get("导入时间", "") or "")
            if it:
                date_part = it[:10]
                if import_after and date_part < import_after:
                    continue
                if import_before and date_part > import_before:
                    continue
        # D93: 订舱代理 — 归一化精确匹配 (不子串, 避免短名/长法人名两批混淆)
        if ba_query:
            stored_ba = _norm(fields.get("订舱代理"))
            if stored_ba != ba_query:
                continue
        # D93: 有效期窗口
        if valid_on or valid_after or valid_before:
            vf = _date_to_iso(fields.get("有效期起"))
            vt = _date_to_iso(fields.get("有效期止"))
            if valid_on:
                if vf and vf > valid_on:
                    continue
                if vt and vt < valid_on:
                    continue
            if valid_after and vt and vt < valid_after:
                continue
            if valid_before and vf and vf > valid_before:
                continue
        filtered.append(r)
    return filtered


def summarize_record(r) -> dict:
    """提取每条记录的关键字段供逐条核对 (D93)."""
    fields = r.get("fields", {}) or {}
    return {
        "record_id": r.get("record_id", ""),
        "pol": str(fields.get("POL", "") or ""),
        "pod": str(fields.get("POD", "") or ""),
        "carrier": str(fields.get("船公司", "") or ""),
        "pc": str(fields.get("P/C", "") or ""),
        "valid_from": _date_to_iso(fields.get("有效期起")),
        "valid_to": _date_to_iso(fields.get("有效期止")),
        "booking_agent": _norm(fields.get("订舱代理")),
        "status": str(fields.get("状态", "") or ""),
        "rate_no": str(fields.get("运价编号", "") or ""),
    }


def find_records(base_token="Eje8bWtVdaPPPosu0GQcPclQnut",
                 table_id="tblnCWVGvCfFHW6m",
                 rate_no="", carrier="", pod="", pol="",
                 import_after="", import_before="",
                 booking_agent="", valid_on="", valid_after="", valid_before=""):
    """返回 (record_ids, err)。write-record/delete-record 内部调用保持兼容."""
    records, err = find_records_detail(
        base_token=base_token, table_id=table_id,
        rate_no=rate_no, carrier=carrier, pod=pod, pol=pol,
        import_after=import_after, import_before=import_before,
        booking_agent=booking_agent, valid_on=valid_on,
        valid_after=valid_after, valid_before=valid_before,
    )
    if err:
        return None, err
    record_ids = [r.get("record_id") for r in records if r.get("record_id")]
    return record_ids, None


def find_records_detail(base_token="Eje8bWtVdaPPPosu0GQcPclQnut",
                        table_id="tblnCWVGvCfFHW6m",
                        rate_no="", carrier="", pod="", pol="",
                        import_after="", import_before="",
                        booking_agent="", valid_on="", valid_after="", valid_before=""):
    """返回 (records_detail, err)，每条含 record_id + 关键字段 (D93)."""
    records, err = list_all_records(base_token, table_id)
    if err:
        return None, err
    filtered = apply_filters(
        records, rate_no, carrier, pod, pol,
        import_after, import_before,
        booking_agent, valid_on, valid_after, valid_before,
    )
    return [summarize_record(r) for r in filtered], None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-token", default="Eje8bWtVdaPPPosu0GQcPclQnut")
    ap.add_argument("--table-id", default="tblnCWVGvCfFHW6m")
    ap.add_argument("--rate-no", default="", help="按运价编号筛选 (支持逗号分隔)")
    ap.add_argument("--carrier", default="", help="按船公司模糊匹配")
    ap.add_argument("--pod", default="", help="按目的港模糊匹配")
    ap.add_argument("--pol", default="", help="按起运港模糊匹配")
    ap.add_argument("--import-after", default="", help="按导入时间起始 (YYYY-MM-DD)")
    ap.add_argument("--import-before", default="", help="按导入时间截止 (YYYY-MM-DD)")
    ap.add_argument("--booking-agent", default="", help="按订舱代理精确匹配 (D93: 归一化去空白; 短名如\"中外运\"/长法人名如\"中外运集装箱运输有限公司\"均直接可查, 不子串模糊)")
    ap.add_argument("--valid-on", default="", help="记录在该日期有效 (YYYY-MM-DD): 有效期起 <= date <= 有效期止")
    ap.add_argument("--valid-after", default="", help="有效期止 >= date (YYYY-MM-DD)")
    ap.add_argument("--valid-before", default="", help="有效期起 <= date (YYYY-MM-DD)")
    args = ap.parse_args()

    detail, err = find_records_detail(
        base_token=args.base_token,
        table_id=args.table_id,
        rate_no=args.rate_no,
        carrier=args.carrier,
        pod=args.pod,
        pol=args.pol,
        import_after=args.import_after,
        import_before=args.import_before,
        booking_agent=args.booking_agent,
        valid_on=args.valid_on,
        valid_after=args.valid_after,
        valid_before=args.valid_before,
    )
    if err:
        print(json.dumps({"ok": False, "code": "FIND_ERROR", "error": err}, ensure_ascii=False))
        sys.exit(1)
    record_ids = [d.get("record_id") for d in detail if d.get("record_id")]
    if not record_ids:
        print(json.dumps({"ok": False, "code": "NO_MATCH", "record_ids": [], "records": [], "count": 0}, ensure_ascii=False))
        sys.exit(0)
    print(json.dumps({
        "ok": True,
        "code": "ok",
        "record_ids": record_ids,
        "records": detail,
        "count": len(record_ids),
    }, ensure_ascii=False, indent=2))
    sys.exit(0)


if __name__ == "__main__":
    main()
