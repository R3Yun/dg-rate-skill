# -*- coding: utf-8 -*-
"""
FCL 海运费表 字段名 -> 飞书 field_id (fldXxx) 映射

2026-08-10 从 /tmp/fcl_fields.json 生成 (lark base +field-list, 41 字段).
用途: lark-cli record-upsert 的 --json payload 键必须是 fld ID,
不能是中文/英文字段名 (否则报 800030201 field not found).

同步: 飞书表结构变更后重新跑:
  lark-cli --as user base +field-list --base-token <BT> --table-id <TID>
  然后用 scripts/internal/_gen_field_ids.py 重新生成本文件 (或手动更新).
"""

FCL_FIELD_ID_MAP = {
    '有效期止': 'fld0O5Wobz',
    '免柜期(天)': 'fldXgWrkbW',
    '航程(天)': 'fld1UUm2YY',
    '40HQ O/F(USD)': 'fldt34a32J',
    'ETD': 'fldmdc3jka',
    'AMS费用': 'fldFjupOqE',
    '40GP DG(USD)': 'fldUYMl5Tk',
    '起运区域': 'fldawROIDw',
    '备注': 'fldxZveF4Z',
    '起运港全称': 'flda1zoW6Z',
    '导入时间': 'fldzw7eIC1',
    'P/C': 'fldbTLNcdA',
    '有效期起': 'fldCZtkrvy',
    'ETA': 'fldQbKTwaA',
    '状态': 'fld5NqEqrn',
    '班期': 'fldZKqcjes',
    'POD': 'fldO48DLjK',
    '目的港全称': 'fldD59VrBG',
    '20GP DG(USD)': 'fldpOkjHyY',
    '船公司': 'fldh2VRr8A',
    '航次': 'fld2vtqFAa',
    '目的区域': 'fldSQ3iDGE',
    '40GP O/F(USD)': 'fld4NZ5Hjf',
    'POL': 'fldKtetOx2',
    '订舱代理': 'fld98P0EV5',
    '45尺 O/F(USD)': 'fldiQnMTug',
    '40NOR O/F(USD)': 'fldueBBbKo',
    '运价类型': 'fldBgcznYZ',
    '直航': 'fldWHV2wHt',
    'VIA中转港': 'fldqqJyVdI',
    '20NOR O/F(USD)': 'fldlshzjYR',
    '原文件附件': 'fldFXom4Zw',
    '运价编号': 'flds8EwiM2',
    '40HQ DG(USD)': 'fldM5sfUJa',
    '超重备注': 'fldyZwTZZQ',
    '解析置信度': 'fldfMHmQem',
    '数据来源': 'fldthltP8N',
    'ENS费用': 'fldQPeXzFU',
    '船名': 'fldufoVzh1',
    '20GP O/F(USD)': 'fldtxsWEd2',
    '合约号': 'fldNek9BEV',
}

# WS-151 事故 (2026-09-01): 英文属性名 → 中文字段名 (反向复用 lark_rate_writer.KEY_ALIAS 语义).
# 插件 merge/update 通路的帮助文案教 LLM 传英文键 (json={pc:"CY",booking_agent:"SNL"}),
# 但 translate_field_keys 只认中文字段名 → 英文键原样透传 → 飞书 800030201 not_found.
# 这里补英文属性名 (含大写变体) → 中文字段名, translate 时先查中文表再查本表.
ATTR_TO_FIELD = {
    # 大写英文 (插件帮助文案常用)
    'POL': 'POL', 'POD': 'POD', 'ETD': 'ETD', 'ETA': 'ETA', 'P/C': 'P/C',
    'PC': 'P/C', 'P_C': 'P/C', 'CARRIER': '船公司', 'LINE': '船公司',
    'VIA_PORT': 'VIA中转港', 'DIRECT': '直航', 'FREQUENCY': '班期',
    'VESSEL': '船名', 'VOYAGE': '航次', 'TT_DAYS': '航程(天)',
    'BOOKING_AGENT': '订舱代理', 'OF_20': '20GP O/F(USD)', 'OF_40': '40GP O/F(USD)',
    'OF_40HQ': '40HQ O/F(USD)', 'OF_20NOR': '20NOR O/F(USD)',
    'OF_40NOR': '40NOR O/F(USD)', 'OF_45': '45尺 O/F(USD)',
    'DG_20': '20GP DG(USD)', 'DG_40': '40GP DG(USD)', 'DG_40HQ': '40HQ DG(USD)',
    'ENS': 'ENS费用', 'AMS': 'AMS费用', 'FREE_TIME': '免柜期(天)',
    'CONTRACT_NO': '合约号', 'VALID_FROM': '有效期起', 'VALID_TO': '有效期止',
    'OWS_NOTE': '超重备注', 'REMARK': '备注', 'STATUS': '状态',
    'CONFIDENCE': '解析置信度', 'IMPORT_TIME': '导入时间',
    'DATA_SOURCE': '数据来源', 'SOURCE_URL': '原文件附件',
    'RATE_TYPE': '运价类型', 'ROL': '起运区域', 'ROD': '目的区域',
    'POL_NAME': '起运港全称', 'POD_NAME': '目的港全称',
    # 小写英文 (KEY_ALIAS 目标名)
    'pol': 'POL', 'pod': 'POD', 'etd': 'ETD', 'eta': 'ETA', 'pc': 'P/C',
    'carrier': '船公司', 'via_port': 'VIA中转港', 'direct': '直航',
    'frequency': '班期', 'vessel': '船名', 'voyage': '航次', 'tt_days': '航程(天)',
    'booking_agent': '订舱代理', 'of_20': '20GP O/F(USD)', 'of_40': '40GP O/F(USD)',
    'of_40hq': '40HQ O/F(USD)', 'of_20nor': '20NOR O/F(USD)',
    'of_40nor': '40NOR O/F(USD)', 'of_45': '45尺 O/F(USD)',
    'dg_20': '20GP DG(USD)', 'dg_40': '40GP DG(USD)', 'dg_40hq': '40HQ DG(USD)',
    'ens': 'ENS费用', 'ams': 'AMS费用', 'free_time': '免柜期(天)',
    'contract_no': '合约号', 'valid_from': '有效期起', 'valid_to': '有效期止',
    'ows_note': '超重备注', 'remark': '备注', 'status': '状态',
    'confidence': '解析置信度', 'import_time': '导入时间',
    'data_source': '数据来源', 'source_url': '原文件附件',
    'rate_type': '运价类型', 'rol': '起运区域', 'rod': '目的区域',
    'pol_name': '起运港全称', 'pod_name': '目的港全称',
}


def translate_field_keys(payload: dict) -> dict:
    """把 payload 的字段名键翻译成飞书 fld ID 键; 未知键原样保留.

    支持: 中文字段名 (POL/船公司/有效期起...) 直接查 FCL_FIELD_ID_MAP;
    英文属性名 (pc/booking_agent/valid_from/carrier...) 经 ATTR_TO_FIELD 转中文再查.
    用于 record-upsert 写文件前, 避免 lark API 报 field not found.
    """
    out = {}
    for k, v in payload.items():
        if not isinstance(k, str):
            out[k] = v
            continue
        fld = FCL_FIELD_ID_MAP.get(k)
        if fld is None:
            cn = ATTR_TO_FIELD.get(k)
            if cn is None:
                cn = ATTR_TO_FIELD.get(k.strip().upper())
            if cn is not None:
                fld = FCL_FIELD_ID_MAP.get(cn)
        out[fld if fld is not None else k] = v
    return out


