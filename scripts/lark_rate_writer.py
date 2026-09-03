# -*- coding: utf-8 -*-
"""
飞书多维表格批量写入模块
通过 SSH 连接 NAS 上的 OpenClaw 容器，使用 lark CLI 写入数据

2026-07-10 增强：
  - 写记录前自动调用 lark_field_helper 给 select 字段（船公司等）补齐选项
  - 自动从 备注 中提取 "船公司:XXX" / "Carrier: XXX" 格式并写入「船公司」字段
  - 关键字段（船公司/POL/POD/运价/有效期/数据来源）缺失时自动降级为「待补充」状态 (v3.7+ 导入人字段已移除)
  - 导入人/审核人 必填为飞书用户 [user][{id: ou_xxx}] 格式
  - 源文件上传到飞书云盘，URL 拼接到「备注」列前（飞书表无独立 URL 字段）
  - 返回报告字段 schema_audit，含用户字段解析、字段补齐、必填校验、源文件上传结果
"""
import json
import hashlib
import os
import re
import subprocess
import sys
import tempfile
import time
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass, field

try:
    import paramiko
    HAS_PARAMIKO = True
except ImportError:
    HAS_PARAMIKO = False

# 2026-08-10 D65: 字段名 -> 飞书 fld ID 翻译 (record-upsert payload 键必须是 fld ID,
# 中文/英文名会报 800030201 field not found)
try:
    from fcl_field_ids import FCL_FIELD_ID_MAP, translate_field_keys
    HAS_FIELD_ID_MAP = True
except ImportError:
    HAS_FIELD_ID_MAP = False
    FCL_FIELD_ID_MAP = {}

    def translate_field_keys(payload):
        return payload


DEFAULT_CONFIG = {
    "nas_host": "192.168.31.128",
    "nas_port": int(os.environ.get("NAS_SSH_PORT", "2122")),
    "nas_user": "admin",
    "nas_password": "Zengs_19761029",
    "container_name": "Openclaw-coco",
    "base_token": "Eje8bWtVdaPPPosu0GQcPclQnut",
    "table_id": "tblnCWVGvCfFHW6m",
    "batch_table_id": "tblmindpCoSaIEsY",   # 导入记录表
    "select_audit": {
        "船公司": ["carrier"],
        "数据来源": ["data_source"],
        "直航": ["direct"],
        "状态": ["status"],
        "目的区域": ["dest_region"],
    },
    # 测试覆盖开关：True 强制走 NAS 路径（docker cp + docker exec），让 _connect mock 能拦截
    # 生产环境保持 False，由运行时检测决定走容器内或 NAS 路径
    "force_nas_path": False,
    # 2026-07-20 D6 拆薄后: 导入记录表已删, batch_writer.py 已删, 批次字段已并入 FCL 表
}

CARRIER_PATTERNS = [
    re.compile(r"船公司\s*[：:=]\s*([A-Za-z][A-Za-z0-9\-_/&\. ]{1,30})"),
    re.compile(r"Carrier\s*[：:=]\s*([A-Za-z][A-Za-z0-9\-_/&\. ]{1,30})", re.I),
    re.compile(r"承运人\s*[：:=]\s*([A-Za-z\u4e00-\u9fa5][A-Za-z0-9\u4e00-\u9fa5\-_/&\. ]{1,30})"),
]


# Entry-side select 值归一化映射（LLM 经常输出 是/否/yes/no 等非 schema 值）
ENTRY_VALUE_NORMALIZERS = {
    "direct": {
        "直航": ["是", "yes", "true", "1", "direct", "Y"],
        "中转": ["否", "no", "false", "0", "transit", "N", ""],
    },
    "status": {
        "待补充": ["待补充", "pending", "incomplete"],
        "已生效": ["已生效", "active", "生效"],
    },
}

VALID_FCL_STATUSES = ("待补充", "已生效")
FCL_STATUS_ALIASES = {
    "pending": "待补充",
    "incomplete": "待补充",
    "active": "已生效",
    "生效": "已生效",
}


def normalize_fcl_status(value, default="已生效"):
    """Return one of the only two valid FCL data-availability statuses."""
    if isinstance(value, list):
        value = value[0] if value else ""
    raw = str(value or "").strip()
    if not raw:
        return default
    normalized = FCL_STATUS_ALIASES.get(raw.lower(), raw)
    if normalized not in VALID_FCL_STATUSES:
        raise ValueError("状态只允许：待补充、已生效")
    return normalized



# P3.1 (2026-07-21): Field name alias map
# LLM outputs non-standard field names (POL/POD/船公司/20GP/ENS费用 etc), which would be silently
# dropped by NormalizedRateEntry.from_dict(). This map translates LLM-friendly names to the
# internal lowercase-English standard names used by the writer.
KEY_ALIAS = {
    # Uppercase English -> lowercase
    "POL": "pol", "POD": "pod", "CARRIER": "carrier", "SHIPPER": "carrier", "LINE": "carrier", "船司": "carrier", "VIA_PORT": "via_port",
    "DIRECT": "direct", "FREQUENCY": "frequency", "VESSEL": "vessel",
    "VOYAGE": "voyage", "ETD": "etd", "ETA": "eta", "TT_DAYS": "tt_days",
    "BOOKING_AGENT": "booking_agent", "OF_20": "of_20", "OF_40": "of_40",
    "OF_40HQ": "of_40hq", "OF_20NOR": "of_20nor", "OF_40NOR": "of_40nor",
    "OF_45": "of_45", "DG_20": "dg_20", "DG_40": "dg_40", "DG_40HQ": "dg_40hq",
    "DG_SURCHARGES": "dg_surcharges", "ENS": "ens", "AMS": "ams",
    "FREE_TIME": "free_time", "PC": "pc", "P/C": "pc", "P_C": "pc",
    "CONTRACT_NO": "contract_no", "VALID_FROM": "valid_from", "VALID_TO": "valid_to",
    "OWS_NOTE": "ows_note", "REMARK": "remark", "STATUS": "status",
    "CONFIDENCE": "confidence", "IMPORT_TIME": "import_time", "DATA_SOURCE": "data_source",
    "RATE_TYPE": "rate_type", "ROL": "rol", "ROD": "rod",
    "CURRENCY": "currency", "SOURCE_FILE": "source_file", "SOURCE_TYPE": "source_type",
    "PARSER": "parser", "WARNINGS": "warnings", "PARSED_AT": "parsed_at",
    "SOURCE_FORMAT": "source_format", "POL_NAME": "pol_name", "POD_NAME": "pod_name",
    "CARRIER_NAME": "carrier_name", "SOURCE_URL": "source_url",
    # Abbreviations (20GP / 40GP / 40HQ)
    "20GP": "of_20", "40GP": "of_40", "40HQ": "of_40hq",
    "20NOR": "of_20nor", "40NOR": "of_40nor", "45尺": "of_45",
    # Feishu field names (with O/F, DG, USD)
    "20GP O/F(USD)": "of_20", "40GP O/F(USD)": "of_40", "40HQ O/F(USD)": "of_40hq",
    "20NOR O/F(USD)": "of_20nor", "40NOR O/F(USD)": "of_40nor", "45尺 O/F(USD)": "of_45",
    "40GP DG(USD)": "dg_40", "40HQ DG(USD)": "dg_40hq", "20GP DG(USD)": "dg_20",
    "20GP DG": "dg_20", "40GP DG": "dg_40", "40HQ DG": "dg_40hq",
    # Chinese
    "运价类型": "rate_type", "起运区域": "rol", "目的区域": "rod",
    "起运港": "pol", "目的港": "pod", "中转港": "via_port",
    "直航": "direct", "船公司": "carrier", "班期": "frequency",
    "船名": "vessel", "航次": "voyage", "航程(天)": "tt_days", "航程": "tt_days",
    "订舱代理": "booking_agent", "合约号": "contract_no",
    "有效期起": "valid_from", "有效期止": "valid_to",
    "免柜期(天)": "free_time", "免柜期": "free_time", "免箱期": "free_time",
    "ENS费用": "ens", "AMS费用": "ams", "超重备注": "ows_note",
    "备注": "remark", "数据来源": "data_source", "原文件附件": "source_url",
    "币种": "currency", "DG附加费": "dg_surcharges", "DG": "dg_surcharges",
    "状态": "status", "解析置信度": "confidence", "导入时间": "import_time",
    "源文件": "source_file", "来源文件": "source_file", "来源原值": "_raw_source",
    "提取的船公司": "carrier_extracted",
}


def _is_un_locode(s):
    """D69/D70: 检测字符串是否已是 5 字符 UN/LOCODE 格式 (如 CNSHA)."""
    import re
    return bool(s) and bool(re.match(r"^[A-Z0-9]{4,5}$", str(s).strip().upper().replace(" ", "")))


def _s(value):
    """D75 (2026-08-20): 安全转字符串, 容忍 bool/int 等非字符串字段值.

    OCR/LLM payload 可能直接带 bool (如 direct: true) 或数值, 原 `(v or "").strip()`
    在 truthy 非字符串上会 AttributeError. 规则:
      None → ""; bool True → "Y" (直航语义), False → ""; 其他非字符串 → str();
      字符串 → strip.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return "Y" if value else ""
    return str(value).strip()


CANONICAL_DATA_SOURCES = (
    "Excel导入", "手动录入", "船司邮件", "文本聊天", "OCR图片", "API同步", "其他",
)


def normalize_data_source(value="", source_path="", source_file="", parser=""):
    """把来源别名/文件信息归一化为飞书「数据来源」正式选项。"""
    raw = str(value or "").strip()
    tokens = " ".join(str(x or "") for x in (raw, source_path, source_file, parser)).lower()
    direct = {
        "excel导入": "Excel导入", "excel": "Excel导入", "xlsx": "Excel导入", "xls": "Excel导入",
        "csv": "Excel导入", "tsv": "Excel导入", "表格": "Excel导入",
        "excel_tier_guide": "Excel导入", "tier_guide": "Excel导入", "cargoware_template": "Excel导入",
        "手动录入": "手动录入", "手工录入": "手动录入", "手工": "手动录入", "人工录入": "手动录入",
        "船司邮件": "船司邮件", "船公司邮件": "船司邮件", "邮件": "船司邮件", "销售邮件": "船司邮件",
        "carrier_email": "船司邮件", "email": "船司邮件", "船司公告": "船司邮件", "船公司公告": "船司邮件",
        "文本聊天": "文本聊天", "文本粘贴": "文本聊天", "text_chat": "文本聊天", "chat_message": "文本聊天",
        "聊天文本": "文本聊天", "粘贴文本": "文本聊天", "txt": "文本聊天",
        "ocr图片": "OCR图片", "ocr": "OCR图片", "图片": "OCR图片", "截图": "OCR图片",
        "image": "OCR图片", "png": "OCR图片", "jpg": "OCR图片", "jpeg": "OCR图片",
        "webp": "OCR图片", "bmp": "OCR图片", "tiff": "OCR图片", "pdf": "OCR图片",
        "api同步": "API同步", "api": "API同步", "接口同步": "API同步", "cargoware_api": "API同步",
        "其他": "其他", "未知": "其他", "unknown": "其他", "测试": "其他", "e2e": "其他",
    }
    if raw in CANONICAL_DATA_SOURCES:
        return raw
    if raw.lower() in direct:
        return direct[raw.lower()]
    if "tier guide" in tokens or "tier_guide" in tokens:
        return "Excel导入"
    if any(x in tokens for x in (".xlsx", ".xls", ".csv", ".tsv", "excel", "表格")):
        return "Excel导入"
    if any(x in tokens for x in (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".pdf", "ocr", "截图")):
        return "OCR图片"
    if any(x in tokens for x in (".txt", "text_chat", "聊天", "粘贴")):
        return "文本聊天"
    if any(x in tokens for x in ("email", "邮件")):
        return "船司邮件"
    if any(x in tokens for x in ("api", "接口")):
        return "API同步"
    return "其他"



def normalize_source_file_type(name=""):
    """把源文件后缀归一化为飞书「来源文件」四种选项。"""
    n = str(name or "").strip().lower()
    if n.endswith((".txt", ".md", ".text", ".log", ".chat")):
        return "文本"
    if n.endswith((".xlsx", ".xls", ".csv", ".tsv")):
        return "表格"
    if n.endswith((".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".webp", ".pdf")):
        return "图片"
    return "其他"


# 飞书 URL 字段格式
URL_FIELD_VALUE = lambda url, text: {"link": url, "text": text or "查看"}


@dataclass
class WriteResult:
    success: bool
    total_count: int = 0
    write_count: int = 0
    error_msg: str = ""
    raw_output: str = ""
    schema_audit: Dict[str, Any] = field(default_factory=dict)
    # 2026-07-16 P1: 缺字段降级（详见 docs/p1-write-lark-missing-field-20260716.md）
    downgraded_count: int = 0       # 缺字段 entry 成功降级为"待补充"的条数 (= v3.8 p1_missing)
    rejected_count: int = 0         # 缺字段 entry 飞书 API 拒收的条数
    missing_records: List[Dict[str, Any]] = field(default_factory=list)
    #   每条 missing_records 格式: {"index": i, "missing_fields": [...], "status": "待补充"}
    # v3.8 新增: P1/P2 缺失分类
    p0_missing_count: int = 0       # P0 阻塞字段缺失的 entry 数 (拒收)
    p1_missing_count: int = 0       # P1 必问字段缺失的 entry 数 (写入「待补充」)
    p2_missing_count: int = 0       # P2 提示字段缺失的 entry 数 (正常入库 + warnings)
    p1_missing_records: List[Dict[str, Any]] = field(default_factory=list)
    #   每条: {"index": i, "missing_fields": ["船公司", "订舱代理", ...]}
    p2_warnings: List[Dict[str, Any]] = field(default_factory=list)
    #   每条: {"index": i, "missing_fields": ["币种", "ETD", ...], "status": "正常入库"}
    record_ids: List[str] = field(default_factory=list)


class LarkRateWriter:
    # v3.8 新增: 单条记录更新 (P1 补救 / P2 合并共用)
    # 用法:
    #   writer.update_record("rec_abc123", {"船公司": "MSK"}, auto_resume_status=True)
    #   writer.merge_record("rec_abc123", {"币种": "USD"})  # P2 同会话合并, 不改 status

    def update_record(self, record_id: str, fields: Dict[str, Any],
                      auto_resume_status: bool = True) -> WriteResult:
        """v3.8 新增: 更新单条记录 (P1 补救)

        - record_id: 飞书 record_id (rec_xxx)
        - fields: 要更新的字段 dict (key=中文字段名 or 属性名, value=值)
        - auto_resume_status: 如果新字段补齐了 P0+P1, 自动改回「已生效」(v3.8.4 实现)

        返回 WriteResult(success, write_count=1, ...).
        """
        return self._record_modify(record_id, fields, mode="update",
                                   auto_resume_status=auto_resume_status)

    def merge_record(self, record_id: str, fields: Dict[str, Any]) -> WriteResult:
        """v3.8 新增: P2 同会话合并 (不改变状态)

        与 update_record 区别: merge_record 不自动改 status, 用于 P2 双轨制
        的「现在补分支」。

        返回 WriteResult(success, write_count=1, ...).
        """
        return self._record_modify(record_id, fields, mode="merge",
                                   auto_resume_status=False)

    def _record_modify(self, record_id: str, fields: Dict[str, Any],
                       mode: str = "update",
                       auto_resume_status: bool = True) -> WriteResult:
        """v3.8 新增: 单条记录修改底层实现

        mode: "update" (P1 补救, 可能改 status) | "merge" (P2 合并, 不改 status)
        """
        if not record_id:
            return WriteResult(success=False, error_msg="record_id is required",
                               write_count=0)

        if not fields:
            return WriteResult(success=False, error_msg="fields is empty",
                               write_count=0)

        # 准备 JSON payload: lark-cli v1.0.70 upsert 接收顶层 field map, 不 wrap fields
        payload_fields = translate_field_keys(fields)
        json_str = json.dumps(payload_fields, ensure_ascii=False)

        # 写到 scratch, 避免命令行长度限制
        try:
            scratch_dir = os.path.expanduser("~/.openclaw/workspace/scratch")
            os.makedirs(scratch_dir, exist_ok=True)
            tmp_name = "upd_" + record_id + "_" + str(int(time.time())) + ".json"
            tmp_local = os.path.join(scratch_dir, tmp_name)
            with open(tmp_local, "w", encoding="utf-8") as _f:
                _f.write(json_str)

            base_token = self.config.get("base_token", "")
            table_id = self.config.get("table_id", "")

            cmd = [
                "lark-cli", "--as", "user",
                "base", "+record-upsert",
                "--base-token", base_token,
                "--table-id", table_id,
                "--record-id", record_id,
                "--json", "@" + os.path.join("scratch", tmp_name),
                "--format", "json",
            ]
            res = subprocess.run(
                cmd,
                cwd="/home/node/.openclaw/workspace",
                capture_output=True, text=True, timeout=60,
            )
            out = (res.stdout or "").strip()
            err = (res.stderr or "").strip()

            if res.returncode != 0:
                return WriteResult(
                    success=False, write_count=0,
                    error_msg="lark-cli exit " + str(res.returncode) + ": " + err[:300],
                    raw_output=out[:1000],
                )

            try:
                data = json.loads(out)
            except Exception:
                return WriteResult(
                    success=False, write_count=0,
                    error_msg="lark-cli 输出非 JSON: " + out[:200],
                    raw_output=out[:1000],
                )

            if not data.get("ok"):
                return WriteResult(
                    success=False, write_count=0,
                    error_msg=data.get("error", {}).get("message", "unknown"),
                    raw_output=out[:1000],
                )

            # v3.8.7: 写入后查回验证 (P0 闸门, 防"upsert 返回 ok 但字段没真写入")
            # 调 record-get 比对本次更新的字段值, 不一致则拒写
            # - merge mode (P2): 同样验证, 业务人员/可可需要知道是否真写入
            # - update mode (P1): 验证后再做 auto_resume
            verify_ok, verify_err = self._record_verify_fields(record_id, fields)
            if not verify_ok:
                return WriteResult(
                    success=False, write_count=0,
                    error_msg="写入后查回失败: " + verify_err,
                    raw_output=out[:1000],
                    schema_audit={
                        "mode": mode,
                        "record_id": record_id,
                        "fields_updated": list(fields.keys()),
                        "verify_error": verify_err,
                    },
                )

            # v3.8.4: auto_resume 真实实现 (P1 补救 → 自动改回"已生效")
            # 逻辑: 仅当 mode=="update" 且 auto_resume_status=True 时
            #       调 record-get 拿当前 status
            #       仅当 status == "待补充" 时, 二次 upsert 把状态改成"已生效"
            # 失败/异常不阻断首次 update 成功, 用 status_change 字典记录
            status_change = None
            if auto_resume_status and mode == "update":
                current_status = self._record_get_status(record_id)
                if current_status == "待补充":
                    ok = self._status_upsert(record_id, "已生效")
                    status_change = {
                        "from": "待补充",
                        "to": "已生效",
                        "applied": ok,
                    }
                elif current_status == "已生效":
                    # 不动, 记录原因
                    status_change = {
                        "from": current_status,
                        "to": current_status,
                        "applied": False,
                        "reason": "non-pending status, not auto-resumed",
                    }
                else:
                    status_change = {
                        "from": current_status,
                        "to": None,
                        "applied": False,
                        "reason": "unknown or null status",
                    }

            schema_audit = {
                "mode": mode,
                "record_id": record_id,
                "fields_updated": list(fields.keys()),
                "auto_resume_status": auto_resume_status,
                "lark_response": data.get("data", {}),
            }
            if status_change is not None:
                schema_audit["status_change"] = status_change

            return WriteResult(
                success=True, write_count=1,
                raw_output=out[:1000],
                schema_audit=schema_audit,
            )
        except subprocess.TimeoutExpired:
            return WriteResult(success=False, write_count=0,
                               error_msg="lark-cli timeout (60s)")
        except Exception as e:
            return WriteResult(success=False, write_count=0,
                               error_msg=type(e).__name__ + ": " + str(e)[:300])

    def _record_get_status(self, record_id: str) -> Optional[str]:
        """v3.8.4 新增: 读出当前记录的「状态」字段值.

        用于 auto_resume 决策: 仅当 status == "待补充" 才 upsert 到「已生效」,
        避免对已经是"已生效"的记录做不必要的状态变更.
        """
        try:
            scratch_dir = os.path.expanduser("~/.openclaw/workspace/scratch")
            cmd = [
                "lark-cli", "--as", "user",
                "base", "+record-get",
                "--base-token", self.config.get("base_token", ""),
                "--table-id", self.config.get("table_id", ""),
                "--record-id", record_id,
                "--format", "json",
            ]
            res = subprocess.run(
                cmd,
                cwd="/home/node/.openclaw/workspace",
                capture_output=True, text=True, timeout=30,
            )
            if res.returncode != 0:
                return None
            data = json.loads(res.stdout or "{}")
            # record-get 返回 list-of-list: data.data[i][idx] 对应 field_id_list[idx]
            items = (data.get("data") or {}).get("data") or []
            fields = (data.get("data") or {}).get("fields") or []
            fid_list = (data.get("data") or {}).get("field_id_list") or []
            if not items or not fid_list:
                return None
            # 找字段 "状态" 或 "Status" 在 fields 列表里的位置
            idx = None
            for i, name in enumerate(fields):
                if name in ("状态", "Status", "status"):
                    idx = i
                    break
            if idx is None:
                return None
            row = items[0]
            if idx >= len(row):
                return None
            val = row[idx]
            # v3.8.4 fix: select 字段是 list ["待补充"], 不是 str
            if isinstance(val, list):
                return val[0] if val else None
            return val if isinstance(val, str) else None
        except Exception:
            return None

    def _status_upsert(self, record_id: str, status: str) -> bool:
        """v3.8.4 新增: 仅写「状态」字段 (auto_resume 内部 helper)."""
        try:
            scratch_dir = os.path.expanduser("~/.openclaw/workspace/scratch")
            os.makedirs(scratch_dir, exist_ok=True)
            ts = int(time.time())
            tmp_name = "status_" + record_id + "_" + str(ts) + ".json"
            tmp_local = os.path.join(scratch_dir, tmp_name)
            with open(tmp_local, "w", encoding="utf-8") as _f:
                json.dump(translate_field_keys({"状态": status}), _f, ensure_ascii=False)

            cmd = [
                "lark-cli", "--as", "user",
                "base", "+record-upsert",
                "--base-token", self.config.get("base_token", ""),
                "--table-id", self.config.get("table_id", ""),
                "--record-id", record_id,
                "--json", "@" + os.path.join("scratch", tmp_name),
                "--format", "json",
            ]
            res = subprocess.run(
                cmd,
                cwd="/home/node/.openclaw/workspace",
                capture_output=True, text=True, timeout=60,
            )
            if res.returncode != 0:
                return False
            try:
                data = json.loads(res.stdout or "{}")
                return bool(data.get("ok"))
            except Exception:
                return False
        except Exception:
            return False

    def _record_verify_fields(self, record_id: str, expected: "Dict[str, Any]") -> "Tuple[bool, str]":
        """v3.8.7 新增: 写入后查回验证 (P0 闸门).

        用于在 record-upsert 返回 ok 之后, 调 record-get 拿回记录, 比对本次更新的字段值
        是否真的写入了. 不一致则返回 (False, error_msg), 防止"upsert 返回 ok 但实际没写入"的
        静默 bug 误导业务人员.

        设计要点:
        1. v3.7 后 FCL 表全部文本字段, 飞书返回 string, 直接 string == 比对
        2. 兼容 select 字段: 飞书返回 ["已生效"], 期望可能是 "已生效" -> _values_equal 处理
        3. 兼容空值: None / "" / [] / {} 都视为等价空
        4. 兼容数值: int/float 字符串互转
        5. 失败 fallback: lark-cli 调用本身失败 -> (False, error), 不当成功

        返回:
        - (True, "") 全部字段一致
        - (False, error_msg) 有不一致字段或调用失败
        """
        if not expected:
            return (True, "")
        # v3.8.7: 等待 lark-cli 字段缓存 (3-5s, AGENTS.md §6)
        # record-upsert 后立即 record-get 会拿到旧值, sleep 4s 兜底
        time.sleep(4)
        try:
            cmd = [
                "lark-cli", "--as", "user",
                "base", "+record-get",
                "--base-token", self.config.get("base_token", ""),
                "--table-id", self.config.get("table_id", ""),
                "--record-id", record_id,
                "--format", "json",
            ]
            res = subprocess.run(
                cmd,
                cwd="/home/node/.openclaw/workspace",
                capture_output=True, text=True, timeout=30,
            )
            if res.returncode != 0:
                return (False, "record-get exit " + str(res.returncode) + ": " + (res.stderr or "")[:200])
            try:
                data = json.loads(res.stdout or "{}")
            except Exception as e:
                return (False, "record-get 输出非 JSON: " + str(e)[:100])
            items = (data.get("data") or {}).get("data") or []
            fields = (data.get("data") or {}).get("fields") or []
            if not items or not fields:
                return (False, "record-get 返回空 data (record_id=" + record_id + ")")
            row = items[0]
            actual = {}
            for i, name in enumerate(fields):
                if i < len(row):
                    actual[name] = row[i]
            mismatched = []
            for k, expected_val in expected.items():
                # D88+ (WS-151 英文键 merge): record-get 返回中文字段名, 而 merge/update
                # payload 可能用英文属性名 (pc/valid_from/booking_agent). 原键不在 actual
                # 时经 ATTR_TO_FIELD 转中文名再比对, 避免"写入成功但查回误报不匹配".
                lookup = k
                if isinstance(k, str) and k not in actual:
                    try:
                        from fcl_field_ids import ATTR_TO_FIELD as _ATTR_TO_FIELD
                        cn = _ATTR_TO_FIELD.get(k)
                        if cn is None:
                            cn = _ATTR_TO_FIELD.get(k.strip().upper())
                        if cn is not None:
                            lookup = cn
                    except Exception:
                        pass
                actual_val = actual.get(lookup)
                if not self._values_equal(actual_val, expected_val):
                    mismatched.append({"field": k, "expected": expected_val, "actual": actual_val})
            if mismatched:
                msg = "写入后查回不匹配: " + json.dumps(mismatched, ensure_ascii=False)
                return (False, msg)
            return (True, "")
        except subprocess.TimeoutExpired:
            return (False, "record-get timeout (30s)")
        except Exception as e:
            return (False, type(e).__name__ + ": " + str(e)[:200])

    @staticmethod
    def _values_equal(actual, expected) -> bool:
        """v3.8.7 新增: 比较两个值是否"等价"(处理 select/list/str/数值/空值差异)."""
        # 1. 完全相等
        if actual == expected:
            return True
        # 2. select 字段: 飞书返回 list ["已生效"], 期望可能是 str "已生效"
        if isinstance(actual, list) and len(actual) == 1 and not isinstance(expected, list):
            return actual[0] == expected
        if isinstance(expected, list) and len(expected) == 1 and not isinstance(actual, list):
            return expected[0] == actual
        # 3. 空值等价
        def _is_empty(v):
            return v in (None, "", [], {})
        if _is_empty(actual) and _is_empty(expected):
            return True
        # 4. 数值: 字符串数字 vs 数字
        try:
            if actual is not None and expected is not None and not isinstance(actual, list) and not isinstance(expected, list):
                if float(actual) == float(expected):
                    return True
        except (TypeError, ValueError):
            pass
        return False

    # v3.8: P0 必填字段 (5 个)    # v3.8: P0 必填字段 (5 个) — 缺失即拒收, 不写入飞书
    #   - pc (P/C) 是 v3 关键字段, 列入 P0
    #   - carrier 不再是必填 (v3.7 改为 P1 必问)
    # data_source 是批次级字段（在 opts 里），不计入 per-entry 必填校验
    REQUIRED_FIELDS = ["pol", "pod", "pc",
                       "valid_from", "valid_to"]

    # v3.8: P1 必问字段 (2 个单字段 + 1 个派生 ≥1 价格) — 写入「待补充」状态
    P1_REQUIRED_FIELDS = ["carrier", "booking_agent"]

    # v3.8 Q3 (2026-07-21): P2 提示字段 (4 个, 收紧) — 写入正常状态, Agent 回复提示
    # 注: currency/vessel/voyage/etd/eta 已不在用户 35-field 飞书表, 移除
    P2_OPTIONAL_FIELDS = ["rol", "rod", "frequency", "contract_no",
                          "vessel", "voyage", "etd", "eta"]

    def __init__(self, config: Dict[str, Any] = None):
        self.config = {**DEFAULT_CONFIG, **(config or {})}
        if not HAS_PARAMIKO:
            raise ImportError("paramiko 未安装")

    def test_connection(self) -> bool:
        try:
            client = self._connect()
            _, stdout, _ = client.exec_command(
                f"sudo docker exec {self.config['container_name']} lark-cli --version"
            )
            version = stdout.read().decode("utf-8", errors="replace").strip()
            client.close()
            print("OK 连接成功！lark 版本:", version)
            return True
        except Exception as e:
            print("连接失败:", str(e))
            return False

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

    def _exec(self, cmd: str, timeout: int = 60) -> str:
        client = self._connect()
        try:
            _, stdout, stderr = client.exec_command(cmd, timeout=timeout)
            out = stdout.read().decode("utf-8", errors="replace").strip()
            err = stderr.read().decode("utf-8", errors="replace").strip()
            return out if out else err
        finally:
            client.close()

    def _write_dedupe_key(self, entries: List[Dict[str, Any]], opts: Dict[str, Any]) -> str:
        """Stable fingerprint for one logical write-lark batch."""
        keys = [
            "pol", "pod", "carrier", "valid_from", "valid_to", "pc",
            "of_20", "of_40", "of_40hq", "of_20nor", "of_40nor", "of_45",
            "dg_surcharges", "source_file", "source_type", "parser",
        ]
        normalized = []
        for entry in entries:
            normalized.append({key: entry.get(key) for key in keys if entry.get(key) not in (None, "", [])})
        payload = {
            # v3.7+: import_user 已移除, dedupe 不再包含
            "source_type": opts.get("source_type", ""),
            "source_url": opts.get("source_url", ""),
            "status": normalize_fcl_status(opts.get("status"), default="已生效"),
            "entries": normalized,
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _dedupe_cache_path(self, key: str) -> str:
        cache_dir = os.environ.get("DG_RATE_WRITE_CACHE_DIR", "/tmp/dg-rate-query-write-cache")
        os.makedirs(cache_dir, exist_ok=True)
        return os.path.join(cache_dir, key + ".json")

    def _check_recent_duplicate(self, key: str, ttl_seconds: int = 600) -> Dict[str, Any]:
        path = self._dedupe_cache_path(key)
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            age = time.time() - float(payload.get("ts", 0))
            if age <= ttl_seconds:
                payload["age_seconds"] = int(age)
                payload["cache_path"] = path
                return payload
        except Exception:
            return {}
        return {}

    def _mark_recent_write(self, key: str, payload: Dict[str, Any]) -> None:
        path = self._dedupe_cache_path(key)
        payload = {**payload, "ts": time.time(), "cache_path": path}
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)

    def _requires_file_write_confirmation(self, opts: Dict[str, Any]) -> bool:
        """File/OCR/PDF sources must be explicitly confirmed before writing."""
        if opts.get("confirm_write") or opts.get("force"):
            return False
        source_type = str(opts.get("source_type", "") or "").lower()
        source_path = str(opts.get("source_path", "") or "").lower()
        source_url = str(opts.get("source_url", "") or "").lower()
        markers = (
            "excel", "xlsx", "xls", "tier", "sheet", "spreadsheet",
            "ocr", "image", "图片", "截图", "pdf", "附件", "file",
        )
        text = " ".join([source_type, source_path, source_url])
        return any(marker in text for marker in markers)

    # ---------- 用户字段解析 ----------
    def _resolve_user_field(self, user_ref: Any, role: str = "import_user") -> Tuple[list, Dict[str, Any]]:
        """把 user_ref 解析成 [{id: ou_xxx}, ...]，给 Bitable user 字段用。

        user_ref 可为：
          - 空 / None / "AI-Agent" / "ai" / "agent" → []（不填）
          - 已是 list → 校验格式
          - "ou_xxx" 字符串 → [{id: "ou_xxx"}]
          - "name1,name2" 字符串 → 拆分
        """
        audit = {"role": role, "input": str(user_ref), "resolved": [], "warnings": []}
        if user_ref is None or user_ref == "":
            return [], audit
        if isinstance(user_ref, list):
            # 假定已经是 [{id: "..."}, ...]
            cleaned = []
            for it in user_ref:
                if isinstance(it, dict) and it.get("id"):
                    cleaned.append({"id": str(it["id"])})
                elif isinstance(it, str) and it:
                    cleaned.append({"id": it})
            audit["resolved"] = cleaned
            return cleaned, audit
        s = str(user_ref).strip()
        if s in ("", "ai", "agent", "AI-Agent", "AI", "bot", "可可"):
            return [], audit
        # 多个 open_id 逗号分隔
        if "," in s or ";" in s or "、" in s:
            parts = re.split(r"[,;、]", s)
            parts = [p.strip() for p in parts if p.strip()]
            cleaned = [{"id": p} for p in parts if p.startswith("ou_") or len(p) >= 6]
            audit["resolved"] = cleaned
            if not cleaned:
                audit["warnings"].append(f"{role} 解析为空（输入 {s!r}）")
            return cleaned, audit
        # 单个 open_id
        if s.startswith("ou_") and len(s) >= 6:
            audit["resolved"] = [{"id": s}]
            return [{"id": s}], audit
        # 名字 → 查 lark_user_helper
        try:
            from lark_user_helper import LarkUserHelper
            h = LarkUserHelper(self.config)
            resolved = h.resolve_user_ref(s)
            audit["resolved"] = resolved
            if not resolved:
                audit["warnings"].append(f"{role} 名字 {s!r} 未解析为 open_id")
            return resolved, audit
        except Exception as e:
            audit["warnings"].append(f"{role} 解析失败: {e}")
            return [], audit

    # ---------- 源文件上传 ----------
    def _upload_source(self, source_path: str) -> Dict[str, Any]:
        """上传源文件到飞书云盘，返回 {file_token, share_url}。"""
        if not source_path or not os.path.isfile(source_path):
            return {"code": "skipped", "msg": "无源文件或文件不存在", "source_path": source_path}
        try:
            from lark_drive_helper import LarkDriveHelper
            h = LarkDriveHelper(self.config)
            r = h.upload(source_path)
            return r
        except Exception as e:
            return {"code": "error", "msg": str(e), "source_path": source_path}

    # ---------- 批次记录 ----------
    def _write_batch_record(
        self,
        source_type: str,
        import_user_field: list = None,  # v3.7+: 已删除, 仅保留兼容
        source_url: str = "",
        source_file_name: str = "",
        parser: str = "",
        confidence: float = 0.0,
        total_count: int = 0,
        success_count: int = 0,
        warning_count: int = 0,
        status: str = "已生效",
        auditor_field: list = None,  # v3.7+: 已删除, 仅保留兼容
        warnings: list = None,
    ) -> Dict[str, Any]:
        """v3.7: 导入记录表已删除, 本函数为 no-op, 返回虚拟 batch_no."""
        import time as _t
        return {
            "code": "ok",
            "batch_no": "v3.7-noop-" + str(int(_t.time())),
            "msg": "batch_record table deleted in v3.7",
            "deleted": True,
        }
    # ---------- 写前预处理 ----------
    def _extract_carrier_from_remark(self, d: Dict[str, Any]) -> Tuple[str, str]:
        """从 备注 中提取 "船公司:XXX" 格式，返回 (extracted_carrier, cleaned_remark)"""
        remark = str(d.get("remark", "") or "")
        if not remark:
            return "", remark
        for pat in CARRIER_PATTERNS:
            m = pat.search(remark)
            if m:
                cand = m.group(1).strip()
                for sep in ("，", ",", " ", "。", "；", ";", "|", "/", " "):
                    if sep in cand:
                        cand = cand.split(sep)[0].strip()
                if cand:
                    cleaned = pat.sub("", remark, count=1).strip(" ，,;；|")
                    return cand, cleaned
        return "", remark

    def _resolve_port_codes(self, d: Dict[str, Any]) -> None:
        """pol/pod/via_port 中文 -> UN/LOCODE 5 码 normalize (2026-07-18 P0-A3).

        飞书 FCL 表 schema 要求 POL/POD/VIA中转港 是英文 UN/LOCODE 5 码 (eg CNSHA / RUVVO)。
        LLM/parse 可能输出 "海参崴" (中文名) / "Vladivostok" (英文名) / 拼写错误 / 别名等。
        未 normalize 会导致飞书 800010407 error "cell value does not match expected input shape"。

        规则:
          1. 已经是 4-5 字符 A-Z0-9 形式 -> 原样保留
          2. 调 PortResolver.resolve() (含 cn/en/alias index + 模糊匹配)
          3. 解析成功 -> 写入原字段, 同时填 *_name (优先官方全称, 回退原值)
          4. 解析失败 -> 原样保留 (外层校验会拒)

        Cost: PortResolver 单例 O(N=3000) 一次冷启动, ~5ms.
        """
        try:
            from port_resolver import _get_default
            resolver = _get_default()
        except Exception:
            return
        for field in ("pol", "pod", "via_port", "booking_agent_unused"):
            pass  # noqa
        for field in ("pol", "pod", "via_port"):
            v = _s(d.get(field))
            if not v:
                continue
            # 已经是 4-5 字符 A-Z0-9 形式
            import re as _re
            if _re.match(r"^[A-Z0-9]{4,5}$", v.upper().replace(" ", "")):
                code = v.upper().replace(" ", "")
                d[field] = code
                # D73 (2026-08-20): LLM 直接传 UN/LOCODE (e.g. "THBKK") 时也补
                # pol_name/pod_name 官方全称. 之前此分支直接 continue, 导致
                # 业务方反馈"目的港全称空"即使 pod="THBKK" 已是正确码.
                # via_port 无 name 字段, 不处理 (与 resolve 路径一致).
                if field in ("pol", "pod") and not d.get(f"{field}_name"):
                    name_field = f"{field}_name"
                    try:
                        en_name = resolver.code_to_en_name(code)
                        cleaned = self._clean_port_name(en_name) if en_name else ""
                        if cleaned:
                            d[name_field] = cleaned
                    except Exception:
                        pass
                    # en_name 缺失 -> 回退原值 (mirror resolve 路径 856-866 行)
                    if not d.get(name_field) and v:
                        d[name_field] = v
                continue
            try:
                code, conf, src_label, original = resolver.resolve(v)
                if code and len(code) in (4, 5):
                    d[field] = code
                    name_field = f"{field}_name"
                    # D70 (2026-08-10): 优先填官方 en_name (去 ", COUNTRY" 后缀),
                    # 回退到原值. 这样简称 "上海" -> "SHANGHAI" (不是 "上海").
                    if field in ("pol", "pod") and not d.get(name_field):
                        try:
                            en_name = resolver.code_to_en_name(code)
                            cleaned = self._clean_port_name(en_name) if en_name else ""
                            if cleaned:
                                d[name_field] = cleaned
                        except Exception:
                            pass
                    # 如果 en_name 缺失, 回退到原值 (简称)
                    if not d.get(name_field) and original:
                        d[name_field] = original
            except Exception:
                continue  # 解析失败保留原值

    @staticmethod
    def _clean_port_name(name):
        """D70 (2026-08-10): 去 port_resolver 官方 en_name 的 ", COUNTRY" 后缀.

        PortResolver 官方 en_name 格式 "SHANGHAI, CHINA" / "TOKYO, JAPAN" 等,
        飞书 FCL 表 目的港全称 期望不带国家后缀的纯城市名.
        """
        if not name:
            return ""
        cleaned = name.strip()
        for sep in (", CHINA", ", INDONESIA", ", VIETNAM",
                    ", THAILAND", ", KOREA", ", JAPAN",
                    ", MALAYSIA", ", PHILIPPINES",
                    ", BANGLADESH", ", IRAN", ", PAKISTAN",
                    ", INDIA", ", SAUDI ARABIA", ", TAIWAN",
                    ", UNITED ARAB EMIRATES", ", SINGAPORE"):
            if cleaned.endswith(sep):
                cleaned = cleaned[:-len(sep)].strip()
                break
        return cleaned

    def _apply_key_alias(self, d):
        # P3.1 (2026-07-21): translate LLM-friendly field names to internal standard names.
        # Without this, NormalizedRateEntry.from_dict() silently drops keys it does not recognize,
        # e.g. POL -> pol, 船公司 -> carrier, 20GP -> of_20.
        if not d:
            return
        canonical = {}
        for k, v in d.items():
            if k in KEY_ALIAS:
                canonical[KEY_ALIAS[k]] = v
            elif k.upper() in KEY_ALIAS:
                canonical[KEY_ALIAS[k.upper()]] = v
            else:
                canonical[k] = v
        d.clear()
        d.update(canonical)

    def _normalize_select_values(self, d: Dict[str, Any]) -> None:
        """归一化 entry 里的 select 字段值到 schema 允许值。是→直航 等。

        复用 config._ENTRY_VALUE_NORMALIZERS 配置。
        """
        norm = ENTRY_VALUE_NORMALIZERS
        for k, mapping in norm.items():
            cur = d.get(k)
            if cur is None:
                continue
            cs = str(cur).strip()
            if not cs:
                continue
            for target, aliases in mapping.items():
                if cs in aliases or cs == target:
                    d[k] = target
                    break

    def _enforce_select_options(self, base_token: str, table_id: str,
                                entries: List[Any]) -> Dict[str, Any]:
        """自动检测表中所有 select 字段，补齐缺选项。

        设计要点：
        - 不依赖 select_audit 配置：拉取 table schema，自动识别 type=select 的字段
        - 兜底：即使 select_audit 配置遗漏，也能补齐
        - 配置中仍保留 select_audit 用于显式声明 entry-side 字段名映射
        """
        audit: Dict[str, Any] = {"select_added": {}, "select_existing": {},
                                  "select_skipped": {}, "warnings": []}
        try:
            from lark_field_helper import LarkFieldHelper
        except Exception as e:
            audit["warnings"].append("LarkFieldHelper 不可用: " + str(e))
            return audit
        helper = LarkFieldHelper(self.config)

        # 1. 从 schema 自动收集所有 select 字段名
        try:
            all_fields = helper.list_fields(base_token, table_id)
        except Exception as e:
            audit["warnings"].append("list_fields 失败: " + str(e))
            all_fields = []
        select_fields = []
        for f in all_fields:
            if (f.get("type") or "").lower() in ("select", "singleselect", "multi-select", "multiselect"):
                nm = f.get("name") or f.get("field_name") or ""
                if nm:
                    select_fields.append(nm)

        # 2. 把配置中显式映射的字段名也并入（防止 schema 没拉到时丢失）
        for fn in self.config.get("select_audit", {}).keys():
            if fn not in select_fields:
                select_fields.append(fn)

        # 3. 对每个 select 字段，提取该字段在 entry 里对应的所有值，补齐
        for field_name in select_fields:
            # 来源映射：先看 config，再看 entry 字段同名
            sources = self.config.get("select_audit", {}).get(field_name, [field_name])
            values = []
            for e in entries:
                d = e.to_dict() if hasattr(e, "to_dict") else (e if isinstance(e, dict) else {})
                for src in sources:
                    v = d.get(src)
                    if v is None:
                        continue
                    v = str(v).strip()
                    if v and v not in values:
                        values.append(v)
            if not values:
                continue
            try:
                res = helper.ensure_options(base_token, table_id, field_name, values)
                audit["select_added"][field_name] = res.get("added", [])
                audit["select_existing"][field_name] = res.get("existing", [])
                audit["select_skipped"][field_name] = res.get("skipped", [])
                if res.get("warnings"):
                    audit["warnings"].extend(
                        [f"{field_name}: " + w for w in res["warnings"]]
                    )
            except Exception as e:
                audit["warnings"].append(f"{field_name} ensure_options 失败: {e}")
        return audit

    def _preprocess_entries(self, entries: List[Any],
                            require_import_user: bool = True) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """v3.8: 规范化 entries + P0/P1/P2 三级字段校验

        - P0 阻塞字段缺失 → 整条不写入, missing_required_fields 列出 (兼容字段)
        - P1 必问字段缺失 → 写入「待补充」状态, p1_missing_records 列出
        - P2 提示字段缺失 → 正常写入, p2_warnings 列出 (Agent 回复里提示)
        - AUTO 字段自动填

        require_import_user: 写入主表时为 True（必填），写导入记录表时为 False
        """
        # v3.8: import rate_io 用于 classify_entry
        from rate_io import classify_entry, NormalizedRateEntry

        audit: Dict[str, Any] = {
            "carrier_extracted_from_remark": 0,
            # v3.8 兼容字段 (老 API 还在用)
            "missing_required_fields": [],   # = p0_missing_records (P0 阻塞)
            "missing_key_fields": [],       # = p1_missing_records (P1 必问)
            "required_field_warnings": [],
            "key_field_warnings": [],
            # v3.8 新增三段
            "p0_missing_records": [],       # P0 阻塞 (拒收)
            "p1_missing_records": [],       # P1 必问 (写入待补充)
            "p2_warnings": [],              # P2 提示 (正常入库 + warnings)
        }
        # v3.7: 自动填导入时间 (Asia/Shanghai, ISO 8601)
        from datetime import datetime
        try:
            from zoneinfo import ZoneInfo
            _now_sh = datetime.now(ZoneInfo("Asia/Shanghai"))
            import_time_iso = _now_sh.strftime("%Y-%m-%dT%H:%M:%S+08:00")
        except Exception:
            import_time_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

        out: List[Dict[str, Any]] = []
        for i, e in enumerate(entries):
            d = e.to_dict() if hasattr(e, "to_dict") else (e if isinstance(e, dict) else {})
            d = dict(d)
            # 2026-07-16 v3.1: 从 POL/POD 自动算 ROL/ROD (起运区域 / 目的区域),
            # 仅在原本空的时候补, 已有的不覆盖. 然后再次 _normalize_select_values
            # 确保 ROL/ROD 在 schema 选项内 (否则清空让用户填).
            try:
                from rate_io import enrich_regions_dict
                enrich_regions_dict(d)
                self._normalize_select_values(d)
            except Exception as _exc:
                pass
            # v3.8: 用 classify_entry 替代旧的 P0/P1 缺失检查逻辑
            # P3.1: translate LLM-friendly field names to internal standard names
            # 必须先做 key alias (POL/pol), 否则 _resolve_port_codes 找不到小写 key
            self._apply_key_alias(d)
            # 2026-07-18 P0-A3: 中文港口名 -> UN/LOCODE 5 码 normalize (在 alias 之后)
            self._resolve_port_codes(d)
            entry = NormalizedRateEntry.from_dict(d)
            cls = classify_entry(entry)
            # 把 p1/p2 标签同步回 dict (便于 _write_batch 写备注)
            d["_p0_missing"] = cls["p0_missing"]
            d["_p1_missing"] = cls["p1_missing"]
            d["_p2_missing"] = cls["p2_missing"]
            d["_classify_status"] = cls["status"]
            # 累积到 audit
            if cls["p0_missing"]:
                audit["p0_missing_records"].append({
                    "index": i, "pol": d.get("pol"), "pod": d.get("pod"),
                    "missing": cls["p0_missing"], "status": "p0_blocked",
                })
                # 兼容老 API
                audit["missing_required_fields"].append({
                    "index": i, "pol": d.get("pol"), "pod": d.get("pod"),
                    "missing": cls["p0_missing"],
                })
            if cls["p1_missing"]:
                audit["p1_missing_records"].append({
                    "index": i, "pol": d.get("pol"), "pod": d.get("pod"),
                    "missing": cls["p1_missing"], "status": "待补充",
                })
                # 兼容老 API (合并 P1 缺失到 missing_key_fields)
                audit["missing_key_fields"].append({
                    "index": i, "pol": d.get("pol"), "pod": d.get("pod"),
                    "missing": cls["p1_missing"],
                })
            if cls["p2_missing"]:
                audit["p2_warnings"].append({
                    "index": i, "pol": d.get("pol"), "pod": d.get("pod"),
                    "missing": cls["p2_missing"], "status": "正常入库",
                })
            # AUTO 字段：写入时间必填；解析置信度未提供时按已完成结构化校验记为 1.0。
            d["import_time"] = import_time_iso
            if d.get("confidence") in (None, ""):
                d["confidence"] = 1.0

            out.append(d)
        # warnings
        if audit["p0_missing_records"]:
            audit["required_field_warnings"].append(
                f"共 {len(audit['p0_missing_records'])} 条记录 P0 阻塞字段缺失，将被拒收"
            )
        if audit["p1_missing_records"]:
            audit["key_field_warnings"].append(
                f"共 {len(audit['p1_missing_records'])} 条记录 P1 必问字段缺失，"
                f"将自动降级为 '待补充' 状态"
            )
        if audit["p2_warnings"]:
            audit["p2_field_warnings"] = [
                f"共 {len(audit['p2_warnings'])} 条记录 P2 提示字段缺失，"
                f"正常入库, Agent 回复里提示业务人员后补"
            ]
        return out, audit

    # ---------- 批量写 ----------
    def _check_carrier_scope(self, entries: List[Any]) -> Optional[Dict[str, Any]]:
        """Reject multi-carrier batches unless one carrier is explicitly confirmed."""
        candidates = set()
        confirmed = set()
        for item in entries:
            if not isinstance(item, dict):
                continue
            for value in item.get("_carrier_candidates", []) or []:
                if str(value).strip():
                    candidates.add(str(value).strip())
            if item.get("_carrier_confirmed"):
                confirmed.add(str(item["_carrier_confirmed"]).strip())
        if len(candidates) > 1 and len(confirmed) != 1:
            return {"code": "MULTI_CARRIER_CONFIRMATION_REQUIRED", "candidates": sorted(candidates), "message": "文件包含多个船公司，必须先明确指定一个船公司后再入库"}
        return None

    def write_rates(self, entries: List[Any], options: Dict[str, Any] = None) -> WriteResult:
        """主入口：写一批运价到 FCL海运费表。

        options 必填/可选：
          - base_token / table_id: 覆盖默认
          - (v3.7+: import_user_id 已移除, 不再必填)
          - auditor_id: 可选，飞书 open_id 或 [{id}]（审核人），不传则留空
          - data_source: 可选，默认"API同步"，写「数据来源」字段
          - status: 默认"已生效"
          - source_path: 可选，源文件路径，写前上传到飞书云盘
          - source_url: 可选，外部已上传的源文件 URL（跳过上传）
        """
        opts = options or {}
        carrier_gate = self._check_carrier_scope(entries)
        if carrier_gate:
            return WriteResult(success=False, error_msg=json.dumps(carrier_gate, ensure_ascii=False), total_count=len(entries), rejected_count=len(entries))
        base_token = opts.get("base_token") or self.config["base_token"]
        table_id = opts.get("table_id") or self.config["table_id"]
        # v3.7+: import_user_id 已移除 (导入人字段已删除)
        import_user_id = ""
        auditor_id = opts.get("auditor_id")
        default_status = normalize_fcl_status(opts.get("status"), default="已生效")
        source_path = opts.get("source_path", "")
        source_url = opts.get("source_url", "")
        batch_size = opts.get("batch_size", 50)
        requested_source = opts.get("data_source") or opts.get("source_type") or ""
        data_source = normalize_data_source(requested_source, source_path=source_path)

        # v3.7: 删导入人/审核人强制校验（运价库单人操作，不再校验 user 字段）

        auditor_id = opts.get("auditor_id")

        # v3.7: 自动填导入时间（ISO 8601 + Asia/Shanghai 时区）
        from datetime import datetime
        try:
            from zoneinfo import ZoneInfo
            _now_sh = datetime.now(ZoneInfo("Asia/Shanghai"))
            import_time_iso = _now_sh.strftime("%Y-%m-%dT%H:%M:%S+08:00")
        except Exception:
            import_time_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

        # v3.7: 解析用户字段 - 字段已删，不再 resolve
        import_user_field = []  # v3.7+: deprecated no-op
        # v3.7+: import_user 已移除, 不再跟踪
        auditor_field = []  # v3.7+: deprecated no-op
        auditor_audit = {"role": "auditor", "resolved": False, "removed_in_v37": True}

        # 2. 先预处理并执行 P0 闸门。任何 P0 缺失都必须在调用飞书 API
        #    之前整批拒收，避免 LLM 把不完整数据写入后再编造成功回复。
        # v3.7+: _preprocess_entries 不再需要 require_import_user 参数
        proc_entries, pre_audit = self._preprocess_entries(entries)

        # 3.5 批次级 source_type -> 每条 entry.data_source
        #     (select 自动补齐通过 entry.data_source 字段才能检测到选项)
        if requested_source:
            normalized_batch_source = normalize_data_source(
                requested_source, source_path=source_path,
            )
            for _e in proc_entries:
                # 批次级 source_type 是调用方事实，不能被 LLM 的 data_source 覆盖。
                if normalized_batch_source:
                    _e["data_source"] = normalized_batch_source

        p0_details = [
            {
                "index": m.get("index"),
                "pol": m.get("pol"),
                "pod": m.get("pod"),
                "missing_fields": list(m.get("missing") or []),
                "status": "p0_blocked",
            }
            for m in pre_audit.get("p0_missing_records", [])
        ]
        if p0_details:
            p1_details = [
                {
                    "index": m.get("index"),
                    "pol": m.get("pol"),
                    "pod": m.get("pod"),
                    "missing_fields": list(m.get("missing") or []),
                    "status": "待补充",
                }
                for m in pre_audit.get("p1_missing_records", [])
            ]
            p2_details = [
                {
                    "index": m.get("index"),
                    "pol": m.get("pol"),
                    "pod": m.get("pod"),
                    "missing_fields": list(m.get("missing") or []),
                    "status": "正常入库",
                }
                for m in pre_audit.get("p2_warnings", [])
            ]
            detail_text = "; ".join(
                f"第{int(m['index']) + 1}条缺少：{', '.join(m['missing_fields'])}"
                for m in p0_details
            )
            error_msg = "P0 必填字段缺失，整批未写入飞书。" + detail_text
            raw = json.dumps(
                {
                    "code": "CRITICAL_FIELDS_MISSING",
                    "success": False,
                    "written": 0,
                    "total": len(proc_entries),
                    "missing_records": p0_details,
                    "p1_missing_records": p1_details,
                    "p2_warnings": p2_details,
                    "error": error_msg,
                },
                ensure_ascii=False,
            )
            return WriteResult(
                success=False,
                total_count=len(proc_entries),
                write_count=0,
                error_msg=error_msg,
                raw_output=raw,
                schema_audit={
                    "preprocess": pre_audit,
                    "p0_gate": {"blocked": True, "atomic": True},
                },
                downgraded_count=0,
                rejected_count=len(p0_details),
                missing_records=p0_details,
                p0_missing_count=len(p0_details),
                p1_missing_count=len(p1_details),
                p2_missing_count=len(p2_details),
                p1_missing_records=p1_details,
                p2_warnings=p2_details,
            )

        # 3. 源文件上传（如有）。P0 闸门通过后才产生外部副作用。
        source_upload = {"code": "skipped", "msg": "无 source_path"}
        if source_path and not source_url:
            source_upload = self._upload_source(source_path)
            if source_upload.get("code") == "ok":
                source_url = source_upload.get("share_url", "")

        if self._requires_file_write_confirmation(opts):
            return WriteResult(
                success=False,
                total_count=len(proc_entries),
                write_count=0,
                error_msg=(
                    "file/OCR/PDF source requires explicit --confirm-write before writing; "
                    "preview parsed entries and ask the user to confirm import first"
                ),
                raw_output=json.dumps({
                    "code": "confirm_required",
                    "message": "文件/OCR/PDF 来源需要用户明确确认后才能写入飞书运价库",
                    "entry_count": len(proc_entries),
                    "source_type": opts.get("source_type", ""),
                }, ensure_ascii=False),
                schema_audit={
                    "confirm_required": True,
                    "preprocess": pre_audit,
                },
                downgraded_count=0, rejected_count=0, missing_records=[],
            )

        # 3.6 幂等保护：Agent 可能在一次会话中重复调用 write-lark。
        # 同一批 entries + 导入人 + 来源类型 10 分钟内只允许真正写库一次。
        dedupe_key = self._write_dedupe_key(proc_entries, opts)
        # v3.8.5 (2026-07-19): 默认 TTL 600s -> 7200s (2h), 防止同会话内长流程反复写库
        # 上限 86400 (24h) 防止 stale 缓存无限期占用
        dedupe_ttl = int(opts.get("dedupe_ttl_seconds", 7200) or 7200)
        if dedupe_ttl > 86400:
            dedupe_ttl = 86400
        if not opts.get("force"):
            duplicate = self._check_recent_duplicate(dedupe_key, dedupe_ttl)
            if duplicate:
                return WriteResult(
                    success=True,
                    total_count=len(proc_entries),
                    write_count=0,
                    error_msg="duplicate write skipped by idempotency guard",
                    raw_output=json.dumps({"code": "duplicate_skipped", "dedupe": duplicate}, ensure_ascii=False),
                    schema_audit={
                        "dedupe": {
                            "skipped": True,
                            "key": dedupe_key,
                            "previous": duplicate,
                            "ttl_seconds": dedupe_ttl,
                        },
                        "preprocess": pre_audit,
                    },
                )

        # 4. 写前 select 字段补齐
        select_audit = self._enforce_select_options(base_token, table_id, proc_entries)

        # 5. 拆分批写入
        total_count = len(proc_entries)
        success_count = 0
        errors = []
        all_outputs = []
        # 2026-07-16 P1: 累积所有 batch 的缺字段降级字段（如果多 batch 需累加）
        accum_downgraded = 0
        accum_rejected = 0
        accum_missing_records = []
        accum_record_ids = []
        accum_p0_missing_count = 0
        accum_p1_missing_count = 0
        accum_p2_missing_count = 0
        for i in range(0, total_count, batch_size):
            batch = proc_entries[i:i + batch_size]
            result = self._write_batch(
                batch, base_token, table_id,
                data_source, default_status, opts,
                pre_audit.get("missing_key_fields", []),
                pre_audit.get("missing_required_fields", []),
                # v3.7+: import_user_field/auditor_field 已移除
                source_url,
            )
            all_outputs.append(result.raw_output)
            if result.success:
                success_count += result.write_count
            else:
                errors.append(result.error_msg)
            # 累积缺字段降级字段
            accum_downgraded += getattr(result, "downgraded_count", 0)
            accum_rejected += getattr(result, "rejected_count", 0)
            accum_missing_records.extend(getattr(result, "missing_records", []))
            accum_record_ids.extend(getattr(result, "record_ids", []))
            accum_p0_missing_count += getattr(result, "p0_missing_count", 0)
            accum_p1_missing_count += getattr(result, "p1_missing_count", 0)
            accum_p2_missing_count += getattr(result, "p2_missing_count", 0)

        # 6. 写 导入记录 表
        avg_conf = 0.0
        warning_total = sum(len((d.get("warnings") or [])) for d in proc_entries)
        for d in proc_entries:
            c = d.get("confidence")
            if isinstance(c, (int, float)):
                avg_conf += float(c)
        if proc_entries:
            avg_conf /= len(proc_entries)
        # 解析器取第一条 entry 的 parser，并统一批次来源值
        parser_name = ""
        if proc_entries:
            parser_name = proc_entries[0].get("parser", "")
        source_type = normalize_data_source(
            opts.get("source_type") or opts.get("data_source") or "",
            source_path=source_path, source_file=source_path, parser=parser_name
        )
        batch_rec = self._write_batch_record(
            # v3.7+: 不再传递 import_user_field
            source_type=source_type,
            source_url=source_url,
            source_file_name=os.path.basename(source_path) if source_path else "",
            parser=parser_name,
            confidence=avg_conf,
            total_count=total_count,
            success_count=success_count,
            warning_count=warning_total,
            status="已生效" if success_count > 0 else "待补充",
            # v3.7+: 不再传递 auditor_field
            warnings=[],
        )

        # 7. 汇总 audit
        schema_audit = {
            # v3.7+: import_user 已移除
            "auditor": auditor_audit,
            "source_upload": source_upload,
            "preprocess": pre_audit,
            "select_options": select_audit,
            "batch_record": batch_rec,
            "dedupe": {"skipped": False, "key": dedupe_key, "ttl_seconds": dedupe_ttl},
        }

        if len(errors) == 0 and success_count > 0:
            self._mark_recent_write(dedupe_key, {
                "total_count": total_count,
                "write_count": success_count,
                "batch_no": batch_rec.get("batch_no", "") if isinstance(batch_rec, dict) else "",
                "source_type": source_type,
            })

        # v3.8: 从 pre_audit 抽取 P0/P1/P2 分类计数
        p0_missing_count = len(pre_audit.get("p0_missing_records", []))
        p1_missing_records = pre_audit.get("p1_missing_records", [])
        p2_warnings = pre_audit.get("p2_warnings", [])
        # 2026-07-16 P1: 返回累积的缺字段降级字段（覆盖所有 batch）
        return WriteResult(
            success=len(errors) == 0,
            total_count=total_count,
            write_count=success_count,
            error_msg="; ".join(errors),
            raw_output="\n".join(all_outputs)[-2000:],
            schema_audit=schema_audit,
            downgraded_count=accum_downgraded,
            rejected_count=accum_rejected,
            missing_records=accum_missing_records,
            # v3.8 新增
            p0_missing_count=accum_p0_missing_count,
            p1_missing_count=accum_p1_missing_count,
            p2_missing_count=accum_p2_missing_count,
            p1_missing_records=p1_missing_records,
            p2_warnings=p2_warnings,
            record_ids=accum_record_ids,
        )

    def preview_records(self, entries: List[Any], options: Dict[str, Any] = None) -> Dict[str, Any]:
        """D69 (2026-08-10): parse + validate + 简称检测 — 不写入 lark.

        返回 dict 供 batch-write.py --preview-only 输出, 业务人员 review 后
        再用 --confirm-write 真正写入. 不调 _write_batch / 不调 lark / 不写 dedupe.
        """
        opts = options or {}
        proc_entries, pre_audit = self._preprocess_entries(entries)
        if requested_source := opts.get("data_source") or opts.get("source_type"):
            normalized_batch_source = normalize_data_source(
                requested_source, source_path=opts.get("source_path", ""),
            )
            for _e in proc_entries:
                if normalized_batch_source:
                    _e["data_source"] = normalized_batch_source
        abbreviations = []
        try:
            from port_resolver import _get_default as _resolver_get_default
            resolver = _resolver_get_default()
        except Exception:
            resolver = None
        for i, _e in enumerate(proc_entries):
            pod = _s(_e.get("pod"))
            pod_name = _s(_e.get("pod_name"))
            if pod and not _is_un_locode(pod):
                abbreviations.append({
                    "index": i, "field": "目的港",
                    "input": pod,
                    "warning": f"目的港 '{pod}' 无法解析为 UN/LOCODE 5 码, 请确认拼写或用官方全称",
                })
            elif pod_name and pod:
                # D70 修正 (2026-08-10): 只有当 pod_name 与官方 en_name 不一致才报简称.
                # 若 _resolve_port_codes 已展开为官方全称 (曼谷→BANGKOK), 不再误报.
                official = ""
                if resolver:
                    try:
                        official = self._clean_port_name(resolver.code_to_en_name(pod))
                    except Exception:
                        official = ""
                if official and official.upper() != pod_name.upper():
                    abbreviations.append({
                        "index": i, "field": "目的港全称",
                        "input": pod_name,
                        "warning": f"目的港全称 '{pod_name}' 与官方全称 '{official}' 不一致, 建议用官方全称",
                    })
            pol = _s(_e.get("pol"))
            pol_name = _s(_e.get("pol_name"))
            if pol and not _is_un_locode(pol):
                abbreviations.append({
                    "index": i, "field": "起运港",
                    "input": pol,
                    "warning": f"起运港 '{pol}' 无法解析为 UN/LOCODE 5 码, 请确认拼写或用官方全称",
                })
            elif pol_name and pol:
                official_pol = ""
                if resolver:
                    try:
                        official_pol = self._clean_port_name(resolver.code_to_en_name(pol))
                    except Exception:
                        official_pol = ""
                if official_pol and official_pol.upper() != pol_name.upper():
                    abbreviations.append({
                        "index": i, "field": "起运港全称",
                        "input": pol_name,
                        "warning": f"起运港全称 '{pol_name}' 与官方全称 '{official_pol}' 不一致, 建议用官方全称",
                    })
        dedupe_key = self._write_dedupe_key(proc_entries, opts)
        dedupe_status = "fresh"
        try:
            from port_resolver import _get_default as _resolver_get_default
            previous = self._check_recent_duplicate(dedupe_key, ttl_seconds=7200)
            dedupe_status = "duplicate" if previous else "fresh"
        except Exception:
            pass
        # D73 (2026-08-20): 为 preview 每条记录追加英文区域/全称 (DISPLAY-ONLY, 加性字段).
        # rol_en/rod_en: region_en(中文 region) -> 英文区域名 (业务方预览可见).
        # pol_name_en/pod_name_en: 镜像 pol_name/pod_name (已为英文官方全称), 确保英文全称始终存在.
        # 不修改/删除任何既有 key (d69 preview 契约: pol_name/pod_name/abbreviations/dedupe_*).
        try:
            from rate_io import region_en
            for _pe in proc_entries:
                if "rol_en" not in _pe:
                    _pe["rol_en"] = region_en(_pe.get("rol", ""))
                if "rod_en" not in _pe:
                    _pe["rod_en"] = region_en(_pe.get("rod", ""))
                if "pol_name_en" not in _pe:
                    _pe["pol_name_en"] = _pe.get("pol_name", "") or ""
                if "pod_name_en" not in _pe:
                    _pe["pod_name_en"] = _pe.get("pod_name", "") or ""
        except Exception:
            pass
        return {
            "total": len(entries),
            "p0_count": len(pre_audit.get("p0_missing_records", [])),
            "p1_count": len(pre_audit.get("p1_missing_records", [])),
            "p2_count": len(pre_audit.get("p2_warnings", [])),
            "records": proc_entries,
            "abbreviations": abbreviations,
            "dedupe_key": dedupe_key,
            "dedupe_status": dedupe_status,
            "p0_missing": pre_audit.get("p0_missing_records", []),
            "p1_missing": pre_audit.get("p1_missing_records", []),
            "p2_warnings": pre_audit.get("p2_warnings", []),
        }

    def _write_batch(self, entries: List[Dict[str, Any]], base_token: str, table_id: str,
                     data_source: str, default_status: str,
                     opts: Dict[str, Any],
                     missing_records: List[Dict[str, Any]],
                     missing_required_records: List[Dict[str, Any]],
                     source_url: str) -> WriteResult:
        # 飞书字段顺序 (2026-07-21 Q3 final+Step10): 与用户当前飞书 UI 顺序一致
        # 用户截图为准 (实测 +field-list 与用户实际不符, 以用户截图作为真值)
        # Q3 Step10 (2026-07-21): +4 vessel/voyage/etd/eta (插在 航程(天) 后, 高频字段)
        # 飞书表共 39 个字段，其中「运价编号」是只读 auto_number；payload 写入其余 38 个字段。
        fields = [
            "运价类型",        # 1  rate_type
            "起运区域",        # 2  rol
            "目的区域",        # 3  rod
            "起运港全称",      # 3+ pol_name (auto-fill from port_resolver)
            "目的港全称",      # 3+ pod_name (auto-fill from port_resolver)
            "POL",             # 4  CRITICAL
            "POD",             # 5  CRITICAL
            "VIA中转港",       # 6  via_port
            "直航",            # 7  direct
            "船公司",          # 8  carrier (P1 必问)
            "班期",            # 9  frequency
            "船名",              # 10 vessel (新位置: 班期后, 航程前)
            "航次",              # 11 voyage
            "ETD",                   # 12 etd
            "ETA",                   # 13 eta
            "航程(天)",        # 14 tt_days (后移, 在 4 个新字段后)
            "订舱代理",        # 15 booking_agent (P1 必问)

            "20GP O/F(USD)",   # 16 of_20
            "40GP O/F(USD)",   # 17 of_40
            "40HQ O/F(USD)",   # 18 of_40hq
            "20NOR O/F(USD)",  # 19 of_20nor
            "40NOR O/F(USD)",  # 20 of_40nor
            "45尺 O/F(USD)",   # 21 of_45
            "40GP DG(USD)",    # 22 dg_40
            "40HQ DG(USD)",    # 23 dg_40hq
            "20GP DG(USD)",    # 24 dg_20
            "ENS费用",         # 25 ens
            "AMS费用",         # 26 ams
            "免柜期(天)",      # 27 free_time
            "P/C",             # 28 CRITICAL
            "合约号",          # 29 contract_no
            "有效期起",        # 30 CRITICAL
            "有效期止",        # 31 CRITICAL
            "超重备注",        # 32 ows_note
            "备注",            # 33 remark
            "状态",            # 34 status (AUTO)
            "解析置信度",      # 35 confidence (AUTO)
            "导入时间",        # 36 import_time (AUTO)
            "数据来源",        # 37 data_source
            "原文件附件",      # 38 batch file URL (不写入每行)
        ]
        # 飞书 select 字段需要 [value] 数组格式；user 字段需要 [{id, name}] 格式
        # v3.7+: FCL 表所有 select 类型字段已改文本 (运价库模块单人操作, 不需要 select 约束)
        # 故 SELECT_FIELDS 保留为空集; 历史 select 字段 (船公司/直航/状态/数据来源/运价类型/起运区域/目的区域/P/C) 都已文本化
        SELECT_FIELDS = set()
        missing_idx = {m["index"]: m["missing"] for m in missing_records}
        missing_required_idx = {m["index"]: m["missing"] for m in missing_required_records}
        rows = []
        p0_missing_records = []
        # v3.10.5.1: p1_missing_records 记录所有 P1 缺失 entry (供审计),
        # downgraded_count 只统计真正降级 (status=待补充) 的 entry,
        # p1_missing_count 统计 P1 缺失字段总数 (sum of len(p1_missing)).
        p1_missing_records = []
        p2_warnings = []
        was_downgraded_total = 0
        p1_missing_fields_total = 0
        for i, d in enumerate(entries):
            p0_missing = list(d.get("_p0_missing") or missing_required_idx.get(i, []))
            p1_missing = list(d.get("_p1_missing") or missing_idx.get(i, []))
            p2_missing = list(d.get("_p2_missing") or [])
            dg_note = ""
            dg_list = d.get("dg_surcharges", [])
            if dg_list:
                dg_notes = []
                for dg in dg_list:
                    if isinstance(dg, dict):
                        fmt = dg.get("format_type", "unified")
                        if fmt == "unified":
                            dg_notes.append(
                                "DG{}/{}".format(dg.get("dg_20", ""), dg.get("dg_40", ""))
                            )
                dg_note = "; ".join(dg_notes)
            extra = []
            cur = d.get("currency", "")
            if cur and cur != "USD":
                extra.append("币种:" + str(cur))
            if dg_note:
                extra.append(dg_note)
            # 缺字段只通过结构化返回和业务回复提示，不污染原始备注。
            # OCR/解析警告
            warns = d.get("warnings") or []
            if isinstance(warns, list):
                for w in warns[:3]:
                    if w:
                        extra.append("⚠️" + str(w)[:80])
            remark = str(d.get("remark", "") or "")
            # v3.10.5.1: Strip LLM-generated pollution patterns (koko fills 备注 with markers)
            # D29 (2026-08-03): +pure numeric strip (防止 LLM 把价格塞进 备注, 如 NO.5755 备注="1500")
            import re as _re
            _POLLUTION = [
                _re.compile(r"⚠️待补充[：:][^|\n]*"),                  # P2 marker block
                _re.compile(r"\(P[012][^)\n]*\)"),                    # (P0/P1/P2 提示) / (P2 提示) etc
                _re.compile(r"\|[ ]*source[：:][^|\n]*"),              # | source: filename (...)
                _re.compile(r"\(re-import[^)\n]*\)"),                  # (re-import #N)
                _re.compile(r"\[[ ]*已提取[：:][^\]]*\]"),             # [已提取: XXX]
                _re.compile(r"⚠️P[012][^|\n]*"),                       # ⚠️P0/⚠️P1/⚠️P2 prefix
                _re.compile(r"^\s*[\d,]+(?:\.\d+)?\s*$"),              # 纯数字/带逗号/带小数 (D29 防价格污染)
            ]
            for _p in _POLLUTION:
                remark = _p.sub("", remark)
            remark = remark.strip(" |").strip()
            if extra:
                sep = " | " if remark else ""
                remark = remark + sep + " ".join(extra)
            src_file = str(d.get("source_file", ""))
            if d.get("parser"):
                src_file = src_file + " [" + str(d["parser"]) + "]"
            # 状态：P0 整条拒收；P1 写入待补充；其余使用默认状态。
            # v3.10.5.1 放宽: 仅缺"订舱代理"时仍标"已生效" (v3.7 决策"运价库单人操作",
            # 订舱代理可在客户订舱时再补, 不阻塞已生效状态). 其他 P1 缺失(船公司/价格)仍标"待补充".
            # v3.10.5.1: 跟踪每条 entry 是否真正降级. 仅有"订舱代理"缺失不降级.
            if p0_missing:
                row_status = "待补充"
                _skip_p0 = True
                _was_downgraded = False  # P0 block 是拒绝, 不是降级
            elif p1_missing and p1_missing != ["订舱代理"]:
                row_status = "待补充"
                _skip_p0 = False
                _was_downgraded = True   # P1 (非订舱代理) 才是真正的降级
            else:
                row_status = default_status
                _skip_p0 = False
                _was_downgraded = False
            def _status_text(v):
                v = (str(v) if v is not None else "").strip()
                normalized = normalize_fcl_status(v) if v else None
                if normalized is None:
                    return None
                return [normalized]

            def _num(v):
                if v is None or v == "":
                    return None
                try:
                    fv = float(v)
                    return int(fv) if fv == int(fv) else fv
                except Exception:
                    return None
            raw_source = str(d.get("data_source") or data_source or "").strip()
            normalized_source = normalize_data_source(
                raw_source, source_file=src_file, parser=d.get("parser", "")
            )
            # v3.10.5.1: removed dead-code extra.append("来源原值:") - extra list never used after remark finalized
            # 把 src_file 后缀名映射到「来源文件」select option (2026-07-16)
            # 把 parser 输出 direct="Y"/"T"/"N"/"F" 映射到 select option (2026-07-16)
            def _direct(v):
                # 直航字段是 text 类型, 返回 plain string 而非 array
                m = _s(v).upper()
                if m in ("Y", "YES", "TRUE", "1"): return "直航"
                if m in ("T", "TRANSIT"): return "中转"
                if m in ("N", "F", "NO", "FALSE", "0"): return "否"
                v2 = _s(v)
                if v2 in ("直航", "中转", "否"): return v2
                return None


            row = [
                str(d.get("rate_type", "") or "FCL3.1"),                  # 1  运价类型 (text)
                str(d.get("rol", "") or ""),                                # 2  起运区域 (text)
                str(d.get("rod", "") or ""),                                # 3  目的区域 (text)
                str(d.get("pol_name", "") or ""),                          # 3+ 起运港全称 (auto-fill)
                str(d.get("pod_name", "") or ""),                          # 3+ 目的港全称 (auto-fill)
                str(d.get("pol", "")),                                       # 4  POL
                str(d.get("pod", "")),                                       # 5  POD
                str(d.get("via_port", "")),                                  # 6  VIA中转港 (text)
                _direct(d.get("direct", "")),                                # 7  直航 (text)
                str(d.get("carrier", "")),                                   # 8  船公司 (text, P1 必问)
                str(d.get("frequency", "") or ""),                           # 9  班期 (text)
                str(d.get("vessel", "") or ""),                               # 10 船名 (text, 新位置)
                str(d.get("voyage", "") or ""),                               # 11 航次 (text)
                str(d.get("etd", "") or ""),                                  # 12 ETD (text)
                str(d.get("eta", "") or ""),                                  # 13 ETA (text)
                _num(d.get("tt_days")),                                      # 14 航程(天) (number, 后移)
                str(d.get("booking_agent", "")),                             # 15 订舱代理 (text, P1 必问)

                _num(d.get("of_20")),                                        # 16 20GP O/F
                _num(d.get("of_40")),                                        # 17 40GP O/F
                _num(d.get("of_40hq")),                                      # 18 40HQ O/F
                _num(d.get("of_20nor")),                                     # 19 20NOR O/F
                _num(d.get("of_40nor")),                                     # 20 40NOR O/F
                _num(d.get("of_45")),                                        # 21 45尺 O/F
                _num(d.get("dg_40")),                                        # 22 40GP DG
                _num(d.get("dg_40hq")),                                      # 23 40HQ DG
                _num(d.get("dg_20")),                                        # 24 20GP DG
                _num(d.get("ens")),                                          # 25 ENS费用
                _num(d.get("ams")),                                          # 26 AMS费用
                _num(d.get("free_time")),                                    # 27 免柜期(天)
                str(d.get("pc", "") or ""),                                  # 28 P/C (text, CRITICAL; 禁止默认 Both)
                str(d.get("contract_no", "") or ""),                         # 29 合约号
                self._fmt_datetime(d.get("valid_from")),                      # 30 有效期起 (CRITICAL, datetime)
                self._fmt_datetime(d.get("valid_to")),                        # 31 有效期止 (CRITICAL, datetime)
                str(d.get("ows_note", "") or ""),                            # 32 超重备注
                remark,                                                        # 33 备注
                _status_text(row_status),                                    # 34 状态 (AUTO, plain text)
                _num(d.get("confidence")),                                   # 35 解析置信度 (AUTO)
                self._fmt_datetime(d.get("import_time")),                     # 36 导入时间 (AUTO, datetime)
                str(normalized_source or ""),                                # 37 数据来源 (text)
                str(d.get("source_url") or source_url or ""),               # 38 原文件附件
            ]
            if p0_missing:
                p0_missing_records.append({
                    "index": i,
                    "pol": d.get("pol"),
                    "pod": d.get("pod"),
                    "missing": p0_missing,
                    "missing_fields": p0_missing,
                    "status": "p0_blocked",
                })
                if p1_missing:
                    p1_missing_records.append({
                        "index": i,
                        "pol": d.get("pol"),
                        "pod": d.get("pod"),
                        "missing": p1_missing,
                        "missing_fields": p1_missing,
                        "status": "已生效",
                    })
                    p1_missing_fields_total += len(p1_missing)
                continue
            if p1_missing:
                p1_missing_records.append({
                    "index": i,
                    "pol": d.get("pol"),
                    "pod": d.get("pod"),
                    "missing": p1_missing,
                    "missing_fields": p1_missing,
                    "status": "待补充" if _was_downgraded else "已生效",
                })
                p1_missing_fields_total += len(p1_missing)
            if p2_missing:
                p2_warnings.append({
                    "index": i,
                    "pol": d.get("pol"),
                    "pod": d.get("pod"),
                    "missing": p2_missing,
                    "missing_fields": p2_missing,
                    "status": "正常入库",
                })
            rows.append(row)
            if _was_downgraded:
                was_downgraded_total += 1

        all_missing_records = p0_missing_records + p1_missing_records + p2_warnings
        if not rows:
            code = "CRITICAL_FIELDS_MISSING" if p0_missing_records else "EMPTY_BATCH"
            error_msg = (
                "P0 必填字段缺失，没有记录写入飞书"
                if p0_missing_records else "没有可写入的记录"
            )
            raw = json.dumps({
                "code": code,
                "success": False,
                "written": 0,
                "total": len(entries),
                "missing_records": all_missing_records,
                "error": error_msg,
            }, ensure_ascii=False)
            return WriteResult(
                False, len(entries), 0, error_msg, raw,
                downgraded_count=was_downgraded_total,
                rejected_count=len(p0_missing_records),
                missing_records=all_missing_records,
                p0_missing_count=len(p0_missing_records),
                p1_missing_count=p1_missing_fields_total,
                p2_missing_count=len(p2_warnings),
                p1_missing_records=p1_missing_records,
                p2_warnings=p2_warnings,
            )

        # v3.8.6 (2026-07-19): 改用 record-upsert 单条循环 (绕过 lark-cli 1.0.72
        # record-batch-create 丢第一条 row 的 bug). payload 是 {field_name: value} 对象列表.
        per_row_payloads = []
        for _row in rows:
            _d = {}
            for _fname, _val in zip(fields, _row):
                if _val is not None and _val != "":
                    _d[_fname] = _val
            per_row_payloads.append(_d)
        payload = per_row_payloads
        json_str = json.dumps(payload, ensure_ascii=False)  # kept for debug dbg path
        # 2026-07-16 修复: 探测当前环境（容器内 vs NAS 主机），选择合适的调用方式
        # 容器内调用：直接 lark-cli；NAS 主机调用：走 docker cp + docker exec
        try:
            client = self._connect()
            try:
                import hashlib
                tmp_name = "dg-write-batch-" + hashlib.md5(json_str.encode("utf-8")).hexdigest()[:12] + ".json"
                # 探测是否在 OpenClaw 容器内直接跑（看 coco scratch 是否可写）
                # 测试覆盖：force_nas_path=True 强制走 NAS 路径，让 _connect mock 能拦截
                scratch_dir = "/home/node/.openclaw/workspace/scratch"
                force_nas = bool(self.config.get("force_nas_path", False))
                if not force_nas and os.path.isdir(os.path.dirname(scratch_dir)) and os.access(os.path.dirname(scratch_dir), os.W_OK):
                    # 容器内路径: 直接调 lark-cli record-upsert 单条循环
                    out = self._call_record_upsert_in_container(payload, base_token, table_id, scratch_dir)
                    err = ""
                    tmp_name = "(unused in v3.8.6)"
                else:
                    # NAS 主机路径: docker cp + docker exec 调 lark-cli record-upsert 单条循环
                    out = self._call_record_upsert_via_ssh(client, payload, base_token, table_id)
                    err = ""
                    tmp_name = "(unused in v3.8.6)"
                    cmd = ""  # keep var
                try:
                    result = json.loads(out)
                    result_data = result.get("data") or {}
                    record_ids = list(
                        result_data.get("record_id_list")
                        or (result_data.get("record") or {}).get("record_id_list")
                        or []
                    )
                    record_ids = [record_id for record_id in record_ids if record_id]
                    if result.get("ok") is not True:
                        error_value = result.get("error")
                        if isinstance(error_value, dict):
                            error_msg = error_value.get("message") or str(error_value)
                        else:
                            error_msg = str(error_value or result)
                        return WriteResult(
                            False, len(entries), 0, error_msg, out,
                            downgraded_count=was_downgraded_total,
                            rejected_count=len(p0_missing_records),
                            missing_records=all_missing_records,
                            p0_missing_count=len(p0_missing_records),
                            p1_missing_count=p1_missing_fields_total,
                            p2_missing_count=len(p2_warnings),
                            p1_missing_records=p1_missing_records,
                            p2_warnings=p2_warnings,
                            record_ids=record_ids,
                        )
                    if len(record_ids) != len(rows):
                        error_msg = (
                            f"飞书返回记录 ID 数 {len(record_ids)} 与待写入数 {len(rows)} 不一致，"
                            "不得宣告写入成功"
                        )
                        return WriteResult(
                            False, len(entries), len(record_ids), error_msg, out,
                            downgraded_count=was_downgraded_total,
                            rejected_count=len(p0_missing_records),
                            missing_records=all_missing_records,
                            p0_missing_count=len(p0_missing_records),
                            p1_missing_count=p1_missing_fields_total,
                            p2_missing_count=len(p2_warnings),
                            p1_missing_records=p1_missing_records,
                            p2_warnings=p2_warnings,
                            record_ids=record_ids,
                        )
                    return WriteResult(
                        True, len(entries), len(record_ids), "", out,
                        downgraded_count=was_downgraded_total,
                        rejected_count=len(p0_missing_records),
                        missing_records=all_missing_records,
                        p0_missing_count=len(p0_missing_records),
                        p1_missing_count=p1_missing_fields_total,
                        p2_missing_count=len(p2_warnings),
                        p1_missing_records=p1_missing_records,
                        p2_warnings=p2_warnings,
                        record_ids=record_ids,
                    )
                except Exception as parse_error:
                    combined = (out + " " + err).lower()
                    fail_markers = (
                        "not found", "command not found", "permission denied", "forbidden",
                        "authentication failed", "rate limit", "internal error", '"ok": false',
                        "code 40", "code 50", "could not",
                    )
                    if any(marker in combined for marker in fail_markers):
                        error_msg = err or out[:200]
                    elif not out and not err:
                        error_msg = "lark-cli 空输出 (PATH 或 binary 不可用)"
                    else:
                        error_msg = "lark-cli 输出非 JSON: " + out[:200]
                    return WriteResult(
                        False, len(entries), 0, error_msg, out,
                        downgraded_count=was_downgraded_total,
                        rejected_count=len(p0_missing_records),
                        missing_records=all_missing_records,
                        p0_missing_count=len(p0_missing_records),
                        p1_missing_count=p1_missing_fields_total,
                        p2_missing_count=len(p2_warnings),
                        p1_missing_records=p1_missing_records,
                        p2_warnings=p2_warnings,
                    )
            finally:
                client.close()
        except Exception as e:
            return WriteResult(
                False, len(entries), 0, str(e),
                downgraded_count=was_downgraded_total,
                rejected_count=len(p0_missing_records),
                missing_records=all_missing_records,
                p0_missing_count=len(p0_missing_records),
                p1_missing_count=p1_missing_fields_total,
                p2_missing_count=len(p2_warnings),
                p1_missing_records=p1_missing_records,
                p2_warnings=p2_warnings,
            )



    def _call_record_upsert_in_container(self, payload, base_token, table_id, scratch_dir):
        """v3.8.6 (2026-07-19): 容器内路径 - 直接 subprocess.run lark-cli record-upsert 单条循环.

        Args:
            payload: list of {field_name: value} dicts
            base_token: lark-cli base token
            table_id: lark-cli table id
            scratch_dir: 容器内 scratch 目录

        Returns:
            str: JSON-encoded result {ok, data: {record_id_list}, errors}

        测试时 mock 这个方法即可, 不会触发实际 subprocess.run.

        D67-A (2026-08-10): record_id 为 None 时自动 dump 完整 payload + cmd + stdout
        到 fail log (scratch_dir/dg-upsert-fail-*.json), 便于 1v1 实测诊断.
        """
        import subprocess as _sp
        import hashlib as _hl
        os.makedirs(scratch_dir, exist_ok=True)
        record_ids_created = []
        upsert_outputs = []
        for _one_payload in payload:
            _one_payload = translate_field_keys(_one_payload)
            _tmp_name = "dg-upsert-" + _hl.md5((json.dumps(_one_payload, ensure_ascii=False, sort_keys=True)).encode("utf-8")).hexdigest()[:12] + ".json"
            _tmp_local = os.path.join(scratch_dir, _tmp_name)
            with open(_tmp_local, "w", encoding="utf-8") as _jf:
                _jf.write(json.dumps(_one_payload, ensure_ascii=False))
            _cmd = [
                "lark-cli", "--as", "user", "base", "+record-upsert",
                "--base-token", base_token, "--table-id", table_id,
                "--json", "@" + os.path.join("scratch", _tmp_name),
                "--format", "json",
            ]
            _res = _sp.run(
                _cmd,
                cwd="/home/node/.openclaw/workspace",
                capture_output=True, text=True, timeout=30
            )
            _out = (_res.stdout or "").strip()
            _err = (_res.stderr or "").strip()
            upsert_outputs.append(_out)
            _rid = None
            _r_parsed = None
            try:
                _r_parsed = json.loads(_out) if _out else json.loads(_err) if _err else None
                if _r_parsed and _r_parsed.get("ok"):
                    _rid = _r_parsed.get("data", {}).get("record", {}).get("record_id_list", [None])[0]
            except Exception:
                pass
            record_ids_created.append(_rid)
            if _rid is None:
                self._dump_upsert_failure(
                    fail_dir=scratch_dir, path_label="container",
                    payload=_one_payload, cmd=_cmd,
                    exit_code=_res.returncode, stderr=_err, stdout_raw=_out,
                    parsed=_r_parsed,
                )
            try:
                os.remove(_tmp_local)
            except Exception:
                pass
        result = {
            "ok": all(rid is not None for rid in record_ids_created),
            "data": {"record_id_list": record_ids_created},
            "errors": [o for o in upsert_outputs if "false" in o.lower()],
        }
        return json.dumps(result, ensure_ascii=False)

    def _dump_upsert_failure(self, *, fail_dir, path_label, payload, cmd,
                             exit_code, stderr, stdout_raw, parsed):
        """D67-A (2026-08-10): 写 upsert fail log 到 fail_dir.

        落盘位置和 payload 文件同目录, 便于 1v1 实测后 SSH 到 NAS/coco
        收集 dg-upsert-fail-*.json 定位真根因.

        Args:
            fail_dir: 落盘目录 (container=scratch_dir, ssh=/tmp on NAS host)
            path_label: "container" or "ssh" 用于诊断区分
            payload: 已 translate_field_keys 后的 payload (fld ID 键)
            cmd: lark-cli 命令 (list 或 str)
            exit_code: 进程退出码 (ssh 路径为 None)
            stderr: 进程 stderr (ssh 路径为空)
            stdout_raw: 进程 stdout 原文
            parsed: 解析后的 stdout JSON 或 None
        """
        try:
            import time as _time
            ts_int = int(_time.time())
            ts_str = _time.strftime("%Y-%m-%dT%H:%M:%S+08:00")
            fail_md5 = hashlib.md5(
                (json.dumps(payload, ensure_ascii=False, sort_keys=True) + str(ts_int)).encode("utf-8")
            ).hexdigest()[:12]
            fail_name = "dg-upsert-fail-" + fail_md5 + ".json"
            fail_path = os.path.join(fail_dir, fail_name)
            if parsed is None:
                reason = "stdout not JSON"
            elif not parsed.get("ok"):
                err = parsed.get("error")
                reason = "lark ok=False (error=" + json.dumps(err, ensure_ascii=False)[:300] + ")"
            else:
                reason = "lark ok=True but record_id_list empty/missing"
            doc = {
                "timestamp": ts_str,
                "path": path_label,
                "cmd": cmd if isinstance(cmd, str) else " ".join(cmd),
                "exit_code": exit_code,
                "stderr": stderr,
                "stdout_parsed": parsed,
                "stdout_raw": stdout_raw,
                "failure_reason": reason,
                "payload": payload,
            }
            os.makedirs(fail_dir, exist_ok=True)
            with open(fail_path, "w", encoding="utf-8") as _f:
                json.dump(doc, _f, ensure_ascii=False, indent=2)
        except Exception as _e:
            sys.stderr.write("dump_upsert_failure failed: " + repr(_e) + "\n")

    def _call_record_upsert_via_ssh(self, client, payload, base_token, table_id):
        """v3.8.6 (2026-07-19): NAS 主机路径 - SSH docker cp + docker exec 调 record-upsert 单条循环.

        Args:
            client: paramiko SSH client (or mock)
            payload: list of {field_name: value} dicts
            base_token: lark-cli base token
            table_id: lark-cli table id

        Returns:
            str: JSON-encoded result {ok, data: {record_id_list}, errors}

        测试时通过 force_nas_path=True 让 _write_batch 走这条路径, 然后 mock 这个方法即可.

        D67-A (2026-08-10): record_id 为 None 时自动 dump fail log 到 /tmp on NAS host
        (与 payload 文件同目录), 1v1 实测后 SSH 到 NAS 直接 cat 诊断.
        """
        import hashlib as _hl
        record_ids_created = []
        upsert_outputs = []
        for _one_payload in payload:
            _one_payload = translate_field_keys(_one_payload)
            _tmp_name = "dg-upsert-" + _hl.md5((json.dumps(_one_payload, ensure_ascii=False, sort_keys=True)).encode("utf-8")).hexdigest()[:12] + ".json"
            _tmp_nas = "/tmp/" + _tmp_name
            with open(_tmp_nas, "w", encoding="utf-8") as _jf:
                _jf.write(json.dumps(_one_payload, ensure_ascii=False))
            _cmd = (
                f"sudo docker cp {_tmp_nas} {self.config['container_name']}:/home/node/.openclaw/workspace/scratch/{_tmp_name} "
                f"&& sudo docker exec -w /home/node/.openclaw/workspace {self.config['container_name']} "
                f"lark-cli --as user base +record-upsert "
                f"--base-token {base_token} --table-id {table_id} "
                f"--json @scratch/{_tmp_name} 2>&1; "
                f"sudo rm -f {_tmp_nas}"
            )
            _, _so, _se = client.exec_command(_cmd)
            _out = _so.read().decode("utf-8", errors="replace").strip()
            upsert_outputs.append(_out)
            _rid = None
            _r_parsed = None
            try:
                _r_parsed = json.loads(_out) if _out else None
                if _r_parsed and _r_parsed.get("ok"):
                    _rid = _r_parsed.get("data", {}).get("record", {}).get("record_id_list", [None])[0]
            except Exception:
                pass
            record_ids_created.append(_rid)
            if _rid is None:
                self._dump_upsert_failure(
                    fail_dir="/tmp", path_label="ssh",
                    payload=_one_payload, cmd=_cmd,
                    exit_code=None, stderr="",
                    stdout_raw=_out, parsed=_r_parsed,
                )
        result = {
            "ok": all(rid is not None for rid in record_ids_created),
            "data": {"record_id_list": record_ids_created},
            "errors": [o for o in upsert_outputs if "false" in o.lower()],
        }
        return json.dumps(result, ensure_ascii=False)

    def _fmt_import_time(self, value):
        """Normalize import_time to an ISO 8601 Asia/Shanghai datetime string."""
        if not value:
            return None
        raw = str(value).strip()
        if not raw:
            return None
        try:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            shanghai = ZoneInfo("Asia/Shanghai")
            normalized = raw.replace("/", "-")
            if len(normalized) == 10:
                parsed = datetime.strptime(normalized, "%Y-%m-%d").replace(tzinfo=shanghai)
            else:
                parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=shanghai)
                else:
                    parsed = parsed.astimezone(shanghai)
            return parsed.isoformat(timespec="seconds")
        except Exception:
            return None

    def _fmt_datetime(self, value):
        """D67-C v2 (2026-08-10 13:30): 格式化 datetime 字段值为 lark 接受格式.

        1v1 事件 #2 + Agent 受控探测都暴露 datetime 字段不能传 dict 包装.
        lark hint 列出的合法形状: string, number, boolean, null, string array for select.
        对于 datetime 字段:
        - date-only (10 字符 "YYYY-MM-DD" 或 "YYYY/MM/DD") -> 纯字符串 "YYYY-MM-DD"
        - 完整 datetime (ISO 8601) -> 纯整数 timestamp_ms

        之前 v1 (D67-C commit 4f44130) 返 {"date": ...} / {"timestamp": ...} dict,
        是 D67-C 单元测试盲区 (测试只验 dict 结构未验 lark 真接受).
        Agent 受控探测 (D67 探测 commit) 通过 D67-A fail log 机制发现此错.

        Returns:
            "YYYY-MM-DD" (str) / timestamp_ms (int) / None
        """
        if not value:
            return None
        raw = str(value).strip()
        if not raw:
            return None
        try:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            shanghai = ZoneInfo("Asia/Shanghai")
            normalized = raw.replace("/", "-")
            if len(normalized) == 10:
                parsed = datetime.strptime(normalized, "%Y-%m-%d").replace(tzinfo=shanghai)
                return parsed.strftime("%Y-%m-%d")
            parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=shanghai)
            else:
                parsed = parsed.astimezone(shanghai)
            return int(parsed.timestamp() * 1000)
        except Exception:
            return None

    def _fmt_price(self, v) -> str:
        if v is None:
            return ""
        try:
            fv = float(v)
        except Exception:
            return str(v)
        return str(int(fv)) if fv == int(fv) else "{:.2f}".format(fv)


def write_rates_to_lark(entries, options=None):
    return LarkRateWriter(options).write_rates(entries, options)


def test_connection():
    return LarkRateWriter().test_connection()


def _cli():
    """CLI 入口：dg-rate-query write-lark 调这里。
    用法:
      python lark_rate_writer.py --entries-json entries.json \
          --source-url https://... --source-type Excel导入
      python lark_rate_writer.py --entries-json entries.json --dry-run
    """
    import argparse
    import sys
    import json as _json

    ap = argparse.ArgumentParser(description="把运价条目写入飞书 FCL 海运费表 (D6 后通常用 write-record/batch-write 调此实现)")
    ap.add_argument("entries_json_pos", nargs="?", default=None, help="(位置参数) entries JSON 文件路径 (兼容旧 parse_file.py 输出 shape)")
    ap.add_argument("--entries-json", dest="entries_json", help="entries JSON 文件路径 (兼容旧 parse_file.py 输出 shape, 与位置参数等价)")
    ap.add_argument("--import-user", default="", help="(已废弃 v3.7) 不再写入飞书")
    ap.add_argument("--auditor", default="", help="(已废弃 v3.7) 不再写入飞书")
    ap.add_argument("--source-url", default="", help="源文件飞书云盘 URL（会拼接到备注列）")
    ap.add_argument("--source-type", default="Excel导入",
                    help="数据来源 (2026-07-12 与 SKILL.md/Docs 同步): "
                         "Excel导入 / excel_tier_guide / OCR图片 / 船司邮件 / "
                         "文本聊天 / 文本粘贴 / 手动录入 / API同步 / 其他")
    ap.add_argument("--status", default="已生效", choices=list(VALID_FCL_STATUSES), help="数据可用状态：待补充 / 已生效；默认 已生效")
    ap.add_argument("--batch-no", default="", help="批次号, 留空自动生成")
    ap.add_argument("--confirm-write", action="store_true", help="确认写入文件/OCR/PDF来源解析结果；无此参数时文件类来源只允许预览不允许入库")
    ap.add_argument("--force", action="store_true", help="绕过短期幂等保护，允许重复写入同一批 entries")
    ap.add_argument("--dry-run", action="store_true", help="只打印不发")
    # 2026-07-18 P0 修复 (方案 γ): --stdin 支持 LLM 直接 pipe JSON, 避免中间文件
    ap.add_argument("--stdin", action="store_true", help="从 stdin 读取 entries JSON (LLM 推荐用法, 替代写临时文件 + 调 raw lark-cli)")
    # 2026-07-18 P0-A3 v3.6 γ+ 强化: --preflight-passed 标记写库前已通过 preflight 校验
    ap.add_argument("--preflight-passed", action="store_true", help="写库前已执行 dg-rate-query preflight 校验的标记; 未传此 flag 时, stderr 输出 warning 提醒 LLM 应当先 preflight")
    args = ap.parse_args()
    entries_json = args.entries_json or args.entries_json_pos
    if not entries_json and not args.stdin:
        ap.error("必须提供 entries JSON: 位置参数 / --entries-json / --stdin")

    # 2026-07-18 P0 γ: stdin 优先于文件路径 (LLM pipe 场景)
    if args.stdin:
        raw = sys.stdin.read()
        if not raw.strip():
            print(_json.dumps({"code":"error","msg":"--stdin 输入为空"}, ensure_ascii=False))
            sys.exit(1)
        try:
            data = _json.loads(raw)
        except _json.JSONDecodeError as e:
            print(_json.dumps({"code":"error","msg":f"stdin JSON 解析失败: {e}"}, ensure_ascii=False))
            sys.exit(1)
        print(_json.dumps({
            "code":"ok_warn",
            "msg":"通过 --stdin 读取 entries JSON (LLM pipe 模式)",
            "stdin_bytes":len(raw)
        }, ensure_ascii=False), file=sys.stderr)
    else:
        with open(entries_json, "r", encoding="utf-8") as f:
            data = _json.load(f)
    # 2026-07-18 P0 修复: 接受多种 JSON shape (LLM hand-crafted 时常用裸数组)
    # - [{"pol":..,"pod":..,...}, ...]                  裸数组 (推荐, LLM 最常生成)
    # - {"entries": [{"pol":..}, ...]}                  旧 parse_file.py 标准输出 wrapper (D6 已删, 保留兼容)
    # - {"data": [{"pol":..}, ...]}                    备用包装
    if isinstance(data, list):
        entries = data
        detected_shape = "bare_array"
    elif isinstance(data, dict):
        if "entries" in data and isinstance(data["entries"], list):
            entries = data["entries"]
            detected_shape = "wrapper_entries"
        elif "data" in data and isinstance(data["data"], list):
            entries = data["data"]
            detected_shape = "wrapper_data"
        else:
            entries = []
            detected_shape = "unknown_dict"
    else:
        entries = []
        detected_shape = "unknown_type"
    if not entries:
        print(_json.dumps({
            "code": "error",
            "msg": "entries is empty / JSON 缺少 entries 或 data 字段",
            "detected_shape": detected_shape,
            "hint": "JSON 必须是以下三种之一: "
                    "[{pol:..,pod:..,...}, ...]  (裸数组, 推荐) | "
                    "{\"entries\": [{...}]}  (旧 parse_file.py 输出 wrapper) | "
                    "{\"data\": [{...}]}  (备用包装) | "
                    "其它 shape 都视为错误, 请重新构造 JSON"
        }, ensure_ascii=False))
        sys.exit(1)
    if detected_shape == "bare_array":
        print(_json.dumps({
            "code": "ok_warn",
            "msg": "接受裸数组 JSON shape (LLM hand-crafted 模式)",
            "detected_shape": detected_shape,
            "entry_count": len(entries)
        }, ensure_ascii=False), file=sys.stderr)

    # 2026-07-18 P0-A3 v3.6 γ+ 强化: 未传 --preflight-passed 时 stderr 警告
    if not args.preflight_passed:
        print(_json.dumps({
            "code": "warn_no_preflight",
            "msg": "v3.6 γ+ 建议: 写库前先调 dg-rate-query preflight --json 文件 校验关键字段; 跳过 preflight 在关键字段缺失时会写库失败或 status=待补充. 加 --preflight-passed 消除此警告.",
            "entry_count": len(entries)
        }, ensure_ascii=False), file=sys.stderr)

    if args.dry_run:
        print(_json.dumps({"code": "ok_dry_run",
                           "entry_count": len(entries),
                           # v3.7+: import_user 已移除
                           "source_url": args.source_url,
                           "sample_entry": entries[0]}, ensure_ascii=False, indent=2))
        return

    options = {
        # v3.7+: import_user / auditor 已移除, 不再写入飞书
        "source_url": args.source_url,
        "source_type": args.source_type,
        "status": args.status,
        "batch_no": args.batch_no,
        "confirm_write": args.confirm_write,
        "force": args.force,
    }
    res = write_rates_to_lark(entries, options)
    out = {
        "code": "ok" if res.success else "error",
        "success": res.success,
        "total": res.total_count,
        "written": res.write_count,
        "batch_no": (res.schema_audit or {}).get("batch_record", {}).get("batch_no", ""),
        "error_msg": res.error_msg,
        "schema_audit": res.schema_audit,
        "missing_records": ((res.schema_audit or {}).get("preprocess", {}) or {}).get("missing_records", []),
    }
    print(_json.dumps(out, ensure_ascii=False, indent=2))
    sys.exit(0 if res.success else 2)


# v3.8 新增: 单条记录更新 / P2 合并 CLI 入口
def _parse_kv_args(items):
    """解析 --field k=v 多次出现为 dict (允许 v 含 =, 用 split('=', 1))"""
    out = {}
    for it in items or []:
        if "=" in it:
            k, v = it.split("=", 1)
            out[k.strip()] = v.strip()
        else:
            # 也支持 JSON 字符串
            try:
                d = json.loads(it)
                if isinstance(d, dict):
                    out.update(d)
            except Exception:
                pass
    return out


def main_update_record():
    """v3.8 新增: dg-rate-query update-record CLI"""
    import argparse
    ap = argparse.ArgumentParser(description="v3.8 P1 补救: 更新单条记录, 可选自动改回「已生效」")
    ap.add_argument("record_id", help="飞书 record_id (rec_xxx)")
    ap.add_argument("--field", action="append", default=[],
                    help="field=value (可多次, e.g. --field 船公司=MSK --field 订舱代理=上海克运)")
    ap.add_argument("--fields-json", default="",
                    help="JSON 字符串 (e.g. --fields-json '{\"船公司\": \"MSK\"}')")
    ap.add_argument("--no-auto-resume", action="store_true",
                    help="不自动改回「已生效」(默认会改)")
    ap.add_argument("--base-token", default="", help="覆盖默认 base_token")
    ap.add_argument("--table-id", default="", help="覆盖默认 table_id")
    args = ap.parse_args()

    # 解析 fields
    fields = _parse_kv_args(args.field)
    if args.fields_json:
        try:
            fields.update(json.loads(args.fields_json))
        except Exception as e:
            print(json.dumps({"code": "error", "msg": "--fields-json 解析失败: " + str(e)},
                             ensure_ascii=False))
            _sys.exit(1)

    if not fields:
        print(json.dumps({"code": "error", "msg": "至少传一个 --field 或 --fields-json"},
                         ensure_ascii=False))
        _sys.exit(1)

    writer = LarkRateWriter()
    if args.base_token:
        writer.config["base_token"] = args.base_token
    if args.table_id:
        writer.config["table_id"] = args.table_id

    result = writer.update_record(args.record_id, fields,
                                  auto_resume_status=not args.no_auto_resume)
    out = {
        "code": "ok" if result.success else "error",
        "record_id": args.record_id,
        "fields_updated": list(fields.keys()),
        "auto_resume_status": not args.no_auto_resume,
        "write_count": result.write_count,
        "error_msg": result.error_msg,
        "schema_audit": result.schema_audit,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    _sys.exit(0 if result.success else 2)


def main_merge_record():
    """v3.8 新增: dg-rate-query write-record --merge CLI (P2 同会话合并)"""
    import argparse
    ap = argparse.ArgumentParser(description="v3.8 P2 合并: 同会话补字段到现有记录 (不改 status)")
    ap.add_argument("record_id", help="飞书 record_id (rec_xxx)")
    ap.add_argument("--field", action="append", default=[],
                    help="field=value (可多次)")
    ap.add_argument("--merge", default="",
                    help="JSON 字符串 (e.g. --merge '{\"币种\": \"USD\", \"ETD\": \"2026-07-25\"}')")
    ap.add_argument("--base-token", default="")
    ap.add_argument("--table-id", default="")
    args = ap.parse_args()

    # 解析 fields
    fields = _parse_kv_args(args.field)
    if args.merge:
        try:
            fields.update(json.loads(args.merge))
        except Exception as e:
            print(json.dumps({"code": "error", "msg": "--merge 解析失败: " + str(e)},
                             ensure_ascii=False))
            _sys.exit(1)

    if not fields:
        print(json.dumps({"code": "error", "msg": "至少传一个 --field 或 --merge"},
                         ensure_ascii=False))
        _sys.exit(1)

    writer = LarkRateWriter()
    if args.base_token:
        writer.config["base_token"] = args.base_token
    if args.table_id:
        writer.config["table_id"] = args.table_id

    result = writer.merge_record(args.record_id, fields)
    out = {
        "code": "ok" if result.success else "error",
        "record_id": args.record_id,
        "fields_merged": list(fields.keys()),
        "write_count": result.write_count,
        "error_msg": result.error_msg,
        "schema_audit": result.schema_audit,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    _sys.exit(0 if result.success else 2)


if __name__ == "__main__":
    import sys as _sys
    if len(_sys.argv) > 1:
        if _sys.argv[1] == "update-record":
            _sys.argv = [_sys.argv[0]] + _sys.argv[2:]
            main_update_record()
        elif _sys.argv[1] == "merge-record":
            _sys.argv = [_sys.argv[0]] + _sys.argv[2:]
            main_merge_record()
        elif _sys.argv[1] != "test-connection":
            _cli()
        else:
            print("测试连接...")
            test_connection()
    else:
        print("测试连接...")
        test_connection()
