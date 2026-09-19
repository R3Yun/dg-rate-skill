# -*- coding: utf-8 -*-
"""
partner_io.py — 合作方（货代/订舱代理/检测机构）主数据 I/O

设计原则 (2026-07-15 v1.0, 曾嵘 P0 决策):
1. 合作方表 = 飞书 Bitable `tbl6KdkpgylccjT7`, 类型选项 v3.2 = 订舱代理/货代/检测机构 (不含客户和船公司)
2. 合作方数据**从 0 维护** (不导入 Cargoware), 由业务人员逐条录入或可可辅助
3. 每条数据有 有效期起/止, 由可可主动推送到期提醒
4. CLI subcommand 复用 dg-rate-query wrapper 入口 (case 分发)

字段说明见 docs/04-rate-management.md §89-117 v3.2 + docs/21-bitable-setup.md §表4
"""

from __future__ import annotations
import json, sys, os, argparse, datetime as dt
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Dict, Tuple

# 飞书 Bitable ID
PARTNER_TABLE_ID = "tbl6KdkpgylccjT7"

# 字段 ID (v1.0, 2026-07-15 实测)
FIELD_NAME        = "fldvn6RSbk"   # 名称 (text)
FIELD_REMARK      = "fldc19jTOI"   # 备注 (text)
FIELD_STATUS      = "fldfgI0dt3"   # 状态 (select: 合作中/已停用)
FIELD_CODE        = "fldPfGPH8b"   # 代码 (text, SCAC/内部代号)
FIELD_CONTACT     = "fldngCkrtt"   # 联系人 (text)
FIELD_CONTACTWAY  = "fldoISUiRe"   # 联系方式 (text)
FIELD_SETTLE      = "fldQM4T3Gs"   # 结算方式 (select: 预付/到付/月结)
FIELD_TYPE        = "fldSzVQl5x"   # 类型 (select: 订舱代理/货代/检测机构)
FIELD_ID_NUM      = "fldJl5LzOr"   # ID (auto_number)
FIELD_CONTRACT    = "fldMeJI8Z8"   # 合同号 (text)
FIELD_VALID_FROM  = "fld1BEzwbV"   # 有效期起 (datetime)
FIELD_VALID_TO    = "fldLiTMNrs"   # 有效期止 (datetime)

# v3.2 类型枚举
TYPE_OPTIONS = ("订舱代理", "货代", "检测机构")
STATUS_OPTIONS = ("合作中", "已停用")


@dataclass
class NormalizedPartner:
    """合作方 (货代/订舱代理/检测机构) 标准结构 — 所有 CLI 子命令都按这个 dataclass 输出"""
    record_id: str = ""              # 飞书 record_id (空 = 新建)
    name: str = ""                   # 名称 (必填)
    type: str = ""                   # 类型 = 订舱代理/货代/检测机构
    code: str = ""                   # 代码 (SCAC/内部代号)
    contact: str = ""                # 联系人
    contact_info: str = ""           # 联系方式 (电话/微信)
    settle_mode: str = ""            # 结算方式 = 预付/到付/月结
    contract_no: str = ""            # 合同号
    valid_from: str = ""             # 有效期起 (ISO "YYYY-MM-DD" 或 ms timestamp)
    valid_to: str = ""               # 有效期止
    status: str = "合作中"           # 合作中/已停用
    note: str = ""                   # 备注
    # 派生 (读时不来自飞书, 由 validity 计算得出)
    days_to_expiry: Optional[int] = None

    # ---------- 业务规则 ----------
    def is_valid_type(self) -> bool:
        return self.type in TYPE_OPTIONS

    def is_valid_status(self) -> bool:
        return self.status in STATUS_OPTIONS

    def is_expiring(self, today: dt.date, days_threshold: int) -> bool:
        """合作方有效期止 在 [today, today+days_threshold] 内 → 视为即将到期"""
        if not self.valid_to:
            return False
        end = _parse_date(self.valid_to)
        if end is None:
            return False
        delta = (end - today).days
        return 0 <= delta <= days_threshold

    def is_expired(self, today: dt.date) -> bool:
        """合作方已过期"""
        if not self.valid_to:
            return False
        end = _parse_date(self.valid_to)
        return end is not None and end < today

    def compute_days_to_expiry(self, today: dt.date) -> None:
        end = _parse_date(self.valid_to) if self.valid_to else None
        self.days_to_expiry = (end - today).days if end else None

    # ---------- 飞书序列化 ----------
    def to_lark_fields(self) -> dict:
        """转成飞书 Bitable fields 格式 (空值不写)"""
        f: Dict[str, object] = {}
        if self.name:         f[FIELD_NAME] = self.name
        if self.type:         f[FIELD_TYPE] = self.type
        if self.code:         f[FIELD_CODE] = self.code
        if self.contact:      f[FIELD_CONTACT] = self.contact
        if self.contact_info: f[FIELD_CONTACTWAY] = self.contact_info
        if self.settle_mode:  f[FIELD_SETTLE] = self.settle_mode
        if self.contract_no:  f[FIELD_CONTRACT] = self.contract_no
        if self.valid_from:
            v = _parse_date(self.valid_from)
            if v: f[FIELD_VALID_FROM] = int(dt.datetime(v.year, v.month, v.day).timestamp() * 1000)
        if self.valid_to:
            v = _parse_date(self.valid_to)
            if v: f[FIELD_VALID_TO] = int(dt.datetime(v.year, v.month, v.day).timestamp() * 1000)
        if self.status:       f[FIELD_STATUS] = self.status
        if self.note:         f[FIELD_REMARK] = self.note
        return f


# ---------- 工具 ----------
def _parse_date(s: str) -> Optional[dt.date]:
    """解析 'YYYY-MM-DD' / 'YYYY/MM/DD' / 'YYYY.MM.DD' / millis-timestamp"""
    if not s:
        return None
    s = str(s).strip()
    # millis timestamp?
    if s.isdigit() and len(s) >= 10:
        try:
            return dt.datetime.fromtimestamp(int(s) / 1000, tz=dt.timezone.utc).replace(tzinfo=None).date()
        except Exception:
            pass
    # ISO 格式
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y%m%d"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def from_lark_record(rec: dict) -> NormalizedPartner:
    """飞书 Bitable 单条记录 → NormalizedPartner.

    接受两种入口:
      A. lark_api 格式: {"fields": {"fld_xxx": value, "名称": value}}
         (用 _str_field 自动识别)
      B. lark-cli markdown 格式: {"fields": {"名称": val, "类型": val, ...},
                                    "record_id": "recXXX"}
    """
    f = rec.get("fields", {}) or {}

    def _str_field(key):
        v = f.get(key)
        if v is None:
            return ""
        # markdown cell 里 select 字段是 '["合作中"]' 字符串
        if isinstance(v, str) and v.startswith("[") and v.endswith("]"):
            inner = v[1:-1].strip()
            parts = [p.strip().strip('"').strip("'") for p in inner.split(",") if p.strip()]
            return parts[0] if parts else ""
        if isinstance(v, list) and v:
            if isinstance(v[0], dict):
                return str(v[0].get("name") or v[0].get("text", "") or "")
            return str(v[0]) if v[0] is not None else ""
        if isinstance(v, dict):
            return str(v.get("name") or v.get("text", "") or "")
        if isinstance(v, (int, float)):
            return str(v)
        return str(v) if v not in (None, "") else ""

    def _date(key):
        v = f.get(key)
        if isinstance(v, (int, float)):
            try:
                return dt.datetime.fromtimestamp(v / 1000, tz=dt.timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d")
            except Exception:
                return str(int(v))
        if isinstance(v, str):
            # markdown cell 里 datetime 是 "2026-07-15 08:00:00"
            return v[:10] if len(v) >= 10 else v
        return ""

    p = NormalizedPartner(
        record_id     = rec.get("record_id", "") or "",
        name          = _str_field("名称"),
        type          = _str_field("类型"),
        code          = _str_field("代码"),
        contact       = _str_field("联系人"),
        contact_info  = _str_field("联系方式"),
        settle_mode   = _str_field("结算方式"),
        contract_no   = _str_field("合同号"),
        valid_from    = _date("有效期起"),
        valid_to      = _date("有效期止"),
        status        = _str_field("状态") or "合作中",
        note          = _str_field("备注"),
    )
    return p


# ---------- 业务过滤 ----------
def filter_expiring(partners: List[NormalizedPartner],
                    days_threshold: int,
                    today: Optional[dt.date] = None,
                    include_expired: bool = False) -> List[NormalizedPartner]:
    """即将到期的合作方 (today <= valid_to <= today+days_threshold)."""
    today = today or dt.date.today()
    out: List[NormalizedPartner] = []
    for p in partners:
        if p.status == "已停用":
            continue
        if p.is_expiring(today, days_threshold):
            p.compute_days_to_expiry(today)
            out.append(p)
        elif include_expired and p.is_expired(today):
            p.compute_days_to_expiry(today)
            out.append(p)
    # 按到期天数升序排序 (快到期的排前面)
    out.sort(key=lambda x: x.days_to_expiry if x.days_to_expiry is not None else 99999)
    return out


# ---------- 飞书读 ----------
def _lark_user_call(args: list, timeout: int = 60) -> str:
    """本地 lark-cli 调用 (as=user), 走当前进程 PATH"""
    import subprocess
    cmd = ["lark-cli", "--as", "user", *args]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"lark-cli failed: {r.stderr or r.stdout[:200]}")
    return r.stdout


def _parse_markdown_table(md: str) -> Tuple[List[str], List[List[str]]]:
    """Parse lark-cli markdown table to (header, rows). Header 第一列是 _record_id."""
    lines = [l for l in md.splitlines() if l.startswith("|")]
    if len(lines) < 3:
        return [], []
    header = [c.strip() for c in lines[0].strip("|").split("|")]
    rows = []
    for ln in lines[2:]:
        cells = [c.strip() for c in ln.strip("|").split("|")]
        # 长度对齐
        if len(cells) < len(header):
            cells += [""] * (len(header) - len(cells))
        elif len(cells) > len(header):
            cells = cells[:len(header)]
        rows.append(cells)
    return header, rows


def read_partners_from_feishu(base_token: str, table_id: str = PARTNER_TABLE_ID,
                              partner_type: str = "") -> List[NormalizedPartner]:
    """从飞书读合作方列表 (lark-cli --as user +record-list --format markdown).

    调用环境: 该函数从 coco / dev 容器内跑, lark-cli 已在 PATH.
    使用 markdown 输出: header 第一列 _record_id 含 record_id (json 输出没有).
    """
    out = _lark_user_call([
        "base", "+record-list",
        "--base-token", base_token,
        "--table-id", table_id,
        "--format", "markdown",
    ])
    header, rows = _parse_markdown_table(out)
    if not header:
        return []
    partners: List[NormalizedPartner] = []
    for cells in rows:
        rec = {"fields": {}, "record_id": ""}
        for col_name, val in zip(header, cells):
            if col_name == "_record_id":
                rec["record_id"] = val
            else:
                # select/multiselect 字段在 markdown 是 ["合作中"] / ["月结","预付"]
                # 但 lark-cli 给的是裸字符串 "合作中" / 空?
                rec["fields"][col_name] = val
        p = from_lark_record(rec)
        partners.append(p)
    if partner_type:
        partners = [p for p in partners if p.type == partner_type]
    return partners


# ---------- 飞书写 ----------
def write_partner_to_feishu(base_token: str, partner: NormalizedPartner,
                            table_id: str = PARTNER_TABLE_ID) -> dict:
    """新增或更新一条合作方记录.
    按 record_id 决定 add vs update; 用 lark-cli --json inline.
    若从 NAS 端调用请确保 lark-cli --as user 已配 OAuth (coCo 容器内可直接使用).
    """
    fields = partner.to_lark_fields()
    if not fields:
        raise ValueError("no fields to write")
    if partner.record_id:
        extra = ["--record-id", partner.record_id]
    else:
        extra = []
    # lark-cli 1.0.67+record-upsert: 无 record-id = create, 有 = update
    out = _lark_user_call([
        "base", "+record-upsert",
        "--base-token", base_token,
        "--table-id", table_id,
        *extra,
        "--json", json.dumps(fields, ensure_ascii=False),
    ])
    try:
        return json.loads(out) if out.strip() else {"ok": True}
    except json.JSONDecodeError as e:
        raise RuntimeError(f"lark-cli record-upsert returned non-JSON: {out[:300]}") from e


# ---------- CLI ----------
def _cli_list(args) -> int:
    base_token = os.environ.get("FEISHU_BITABLE_APP_TOKEN", "")
    partners = read_partners_from_feishu(base_token, partner_type=args.type or "")
    out = [asdict(p) for p in partners]
    print(json.dumps({"ok": True, "count": len(out), "partners": out},
                     ensure_ascii=False, indent=2))
    return 0


def _cli_add(args) -> int:
    base_token = os.environ.get("FEISHU_BITABLE_APP_TOKEN", "")
    p = NormalizedPartner(
        name        = args.name or "",
        type        = args.type or "",
        code        = args.code or "",
        contact     = args.contact or "",
        contact_info= args.contact_info or "",
        settle_mode = args.settle_mode or "",
        contract_no = args.contract_no or "",
        valid_from  = args.valid_from or "",
        valid_to    = args.valid_to or "",
        status      = args.status or "合作中",
        note        = args.note or "",
    )
    if not p.is_valid_type():
        print(json.dumps({"ok": False, "error": f"类型必须是 {TYPE_OPTIONS}"},
                         ensure_ascii=False))
        return 2
    res = write_partner_to_feishu(base_token, p)
    print(json.dumps({"ok": res.get("ok", True), "data": res.get("data")},
                     ensure_ascii=False, indent=2))
    return 0


def _cli_expiring(args) -> int:
    base_token = os.environ.get("FEISHU_BITABLE_APP_TOKEN", "")
    partners = read_partners_from_feishu(base_token)
    days = int(args.days)
    expiring = filter_expiring(partners, days, include_expired=args.include_expired)
    out = [asdict(p) for p in expiring]
    print(json.dumps({"ok": True, "today": str(dt.date.today()),
                      "threshold_days": days, "count": len(out),
                      "partners": out}, ensure_ascii=False, indent=2))
    return 0


def _cli_lookup(args) -> int:
    base_token = os.environ.get("FEISHU_BITABLE_APP_TOKEN", "")
    partners = read_partners_from_feishu(base_token)
    name = (args.name or "").strip().lower()
    matches = [p for p in partners if name in p.name.lower() or name in p.code.lower()]
    out = [asdict(p) for p in matches]
    print(json.dumps({"ok": True, "count": len(out), "partners": out},
                     ensure_ascii=False, indent=2))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="dg-rate-query partners",
        description="合作方 (货代/订舱代理/检测机构) 主数据管理 — 飞书 Bitable",
    )
    sub = parser.add_subparsers(dest="action", required=True)

    p_list = sub.add_parser("list", help="列出全部合作方")
    p_list.add_argument("--type", choices=list(TYPE_OPTIONS), help="按类型过滤")
    p_list.set_defaults(func=_cli_list)

    p_add = sub.add_parser("add", help="新增合作方")
    p_add.add_argument("--name", required=True)
    p_add.add_argument("--type", required=True, choices=list(TYPE_OPTIONS))
    p_add.add_argument("--code", help="代码 / SCAC")
    p_add.add_argument("--contact", help="联系人")
    p_add.add_argument("--contact-info", help="联系方式 (电话/微信)")
    p_add.add_argument("--settle-mode", choices=list(("预付", "到付", "月结")))
    p_add.add_argument("--contract-no", help="合同号")
    p_add.add_argument("--valid-from", help="有效期起 (YYYY-MM-DD)")
    p_add.add_argument("--valid-to",   help="有效期止 (YYYY-MM-DD)")
    p_add.add_argument("--status", choices=list(STATUS_OPTIONS), default="合作中")
    p_add.add_argument("--note", help="备注")
    p_add.set_defaults(func=_cli_add)

    p_exp = sub.add_parser("expiring", help="即将到期 (默认 30 天)")
    p_exp.add_argument("--days", type=int, default=30, help="到期阈值 (天)")
    p_exp.add_argument("--include-expired", action="store_true", help="包含已过期")
    p_exp.set_defaults(func=_cli_expiring)

    p_lk = sub.add_parser("lookup", help="按名称 / 代码 查")
    p_lk.add_argument("name")
    p_lk.set_defaults(func=_cli_lookup)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
