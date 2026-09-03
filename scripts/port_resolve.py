#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""port_resolve — D6 工具类原子工具 (D6-6)

文件名用下划线而非横杠, 解决 port-batch-resolve 直接 import.

单港解析: UN/LOCODE 或中文港口名 → 元信息.

CLI:
  port-resolve CNSHA                    # 子命令名仍带 hyphen (wrapper 转换)
  port-resolve --json '{"unlocode":"CNSHA"}'

输出: {code, name_cn, name_en, region_cn, country_cn, country_iso, is_main_port, source}

Exit codes:
  0 — 命中
  1 — not_found 或空查询
  2 — 参数错误
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _field(port, *keys):
    for k in keys:
        v = port.get(k)
        if v not in (None, ""):
            return v
    return None


def resolve(query):
    """单港解析, 返回 dict. 兼容 UN/LOCODE, cn_name, en_name."""
    try:
        from port_resolver import PortResolver
        pr = PortResolver()
    except Exception as e:
        return {"query": query, "error": f"PortResolver load failed: {e}"}

    if not query:
        return {"query": query, "error": "empty query"}

    q = query.strip()
    port = None
    q_upper = q.upper()

    if q_upper in pr.by_code:
        port = pr.by_code[q_upper]
    else:
        for p in pr.ports:
            if str(p.get("cn_name", "")).strip() == q:
                port = p
                break
        if not port:
            for p in pr.ports:
                if str(p.get("en_name", "")).strip().upper() == q_upper:
                    port = p
                    break
        if not port and q_upper in pr.alias_index:
            code = pr.alias_index[q_upper]
            if code in pr.by_code:
                port = pr.by_code[code]

    if not port:
        return {"query": query, "code": None, "not_found": True, "source": pr._data_source}

    return {
        "query": query,
        "code": port.get("code"),
        "name_cn": _field(port, "cn_name", "Port_CN"),
        "name_en": _field(port, "en_name", "Port_EN"),
        "region_cn": _field(port, "region", "Route_CN", "Region_CN"),
        "country_cn": _field(port, "country", "Country_CN"),
        "country_iso": _field(port, "iso_code", "Country_ISO"),
        "is_main_port": _field(port, "base_port", "Is_Main_Port"),
        "source": pr._data_source,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query", nargs="?", default=None)
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    if args.json:
        try:
            payload = json.loads(args.json)
            args.query = payload.get("query") or payload.get("unlocode") or payload.get("name")
        except Exception as e:
            print(json.dumps({"error": f"invalid --json: {e}"}, ensure_ascii=False))
            sys.exit(2)

    result = resolve(args.query)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if not result.get("error") and not result.get("not_found") else 1)


if __name__ == "__main__":
    main()