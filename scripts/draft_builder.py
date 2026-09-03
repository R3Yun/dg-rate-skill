#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build normalized rate drafts from persisted workbook rows and a field mapping."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from parse_workspace import ParseWorkspace, StaleWorkspaceError
from rate_io import NormalizedRateEntry, classify_entry


MAPPING_SCHEMA_VERSION = "rate-field-mapping/v1"
DRAFT_SCHEMA_VERSION = "rate-draft/v1"
ALLOWED_TRANSFORMS = {"strip", "upper", "number", "rate_number"}
FIELD_NAMES = set(NormalizedRateEntry.__dataclass_fields__.keys())
PRICE_FIELDS = {"of_20", "of_40", "of_40hq", "of_20nor", "of_40nor", "of_45"}
# v3.10.6.2 (D17): split PRICE_FIELDS into standard vs NOR for NOR-only detection.
STANDARD_PRICE_FIELDS = {"of_20", "of_40", "of_40hq"}
NOR_PRICE_FIELDS = {"of_20nor", "of_40nor"}
_COLUMN_RE = re.compile(r"^[A-Z]+$")
# 检测字段名是否含中文, 用于在非法字段错误里追加中文别名提示
_CJK_RE = re.compile(r"[\u3400-\u9fff]")
MAPPING_FIELD_ALIASES = {
    "POL": "pol", "POD": "pod", "P/C": "pc", "PC": "pc",
    "船公司": "carrier", "订舱代理": "booking_agent",
    "20GP": "of_20", "40GP": "of_40", "40HQ": "of_40hq",
    "20NOR": "of_20nor", "40NOR": "of_40nor", "45尺": "of_45",
    "PRICE20GP": "of_20", "PRICE40GP": "of_40", "PRICE40HQ": "of_40hq",
    "起运区域": "rol", "目的区域": "rod", "币种": "currency",
    "有效期起": "valid_from", "有效期止": "valid_to", "备注": "remark",
    "船名": "vessel", "Vessel": "vessel", "VESSEL": "vessel",
    "航次": "voyage", "Voyage": "voyage", "VOYAGE": "voyage",
    "ETD": "etd", "Etd": "etd",
    "ETA": "eta", "Eta": "eta",
    "航程(天)": "tt_days", "航程": "tt_days", "T/T": "tt_days", "TT": "tt_days", "Transit Time": "tt_days",
    "直航": "direct", "Direct": "direct", "DIRECT": "direct",
    "班期": "frequency", "Frequency": "frequency", "FREQUENCY": "frequency",
    "VIA中转港": "via_port", "中转港": "via_port", "Via Port": "via_port", "VIA": "via_port",
    "40GP DG(USD)": "dg_40", "40HQ DG(USD)": "dg_40hq", "20GP DG(USD)": "dg_20",
    "DG40GP": "dg_40", "DG40HQ": "dg_40hq", "DG20GP": "dg_20",
    "合约号": "contract_no", "Contract No": "contract_no", "ContractNo": "contract_no",
    "免柜期(天)": "free_time", "免柜期": "free_time", "Free Time": "free_time", "FreeTime": "free_time",
    "ENS费用": "ens", "ENS": "ens",
    "AMS费用": "ams", "AMS": "ams",
    "超重备注": "ows_note", "OWS": "ows_note",
}
TRANSFORM_ALIASES = {"int": "number", "float": "number", "numeric": "number"}


class MappingValidationError(ValueError):
    code = "INVALID_MAPPING"


def _normalize_mapping_field_name(value: Any) -> str:
    name = str(value or "").strip()
    if name in FIELD_NAMES:
        return name
    return MAPPING_FIELD_ALIASES.get(name) or MAPPING_FIELD_ALIASES.get(name.upper()) or name


def _field_names_hint() -> str:
    """返回排序后的合法字段名列表字符串, 用于错误消息提示 LLM/可可用字段. """
    return ", ".join(sorted(FIELD_NAMES))


def _alias_hint(raw_field_name: Any) -> str:
    """若 raw_field_name 为中文且不可解析, 返回中文别名提示串; 否则返回空串.

    帮助 LLM/可可学习正确的原始字段名, 避免使用 "航线" 这类不在别名表的字段.
    采样最多 12 条, 超出用 " ..." 表示还有更多.
    """
    name = str(raw_field_name or "")
    if not _CJK_RE.search(name):
        return ""
    chinese_aliases = [
        f"{alias}->{target}"
        for alias, target in MAPPING_FIELD_ALIASES.items()
        if _CJK_RE.search(alias)
    ]
    if not chinese_aliases:
        return ""
    sample = ", ".join(chinese_aliases[:12])
    suffix = " ..." if len(chinese_aliases) > 12 else ""
    return f" Chinese aliases exist: {sample}{suffix}"


def _column_index(column: str) -> int:
    value = str(column or "").strip().upper()
    if not _COLUMN_RE.fullmatch(value):
        raise MappingValidationError(f"invalid Excel column: {column!r}")
    result = 0
    for character in value:
        result = result * 26 + ord(character) - 64
    return result


def _atomic_write_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.tmp-{uuid.uuid4().hex}"
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as output:
            for row in rows:
                output.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _load_json_or_value(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return json.loads(json.dumps(value, ensure_ascii=False))
    path = str(value or "").strip()
    if path.startswith("{"):
        try:
            loaded = json.loads(path)
        except json.JSONDecodeError as exc:
            raise MappingValidationError(f"invalid inline mapping JSON: {exc}") from exc
        if not isinstance(loaded, dict):
            raise MappingValidationError("mapping must be a JSON object")
        return loaded
    if path.startswith("@"):
        path = path[1:]
    with open(path, "r", encoding="utf-8") as source:
        loaded = json.load(source)
    if not isinstance(loaded, dict):
        raise MappingValidationError("mapping must be a JSON object")
    return loaded


def _load_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as source:
        for line_number, line in enumerate(source, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise MappingValidationError(f"invalid raw JSONL line {line_number}: {exc}") from exc
            if not isinstance(value, dict):
                raise MappingValidationError(f"raw JSONL line {line_number} is not an object")
            yield value


def validate_mapping(mapping: Dict[str, Any], workbook: Dict[str, Any], parse_id: str) -> Dict[str, Any]:
    if mapping.get("schema_version") not in (None, MAPPING_SCHEMA_VERSION):
        raise MappingValidationError("unsupported mapping schema_version")
    if mapping.get("parse_id") not in (None, parse_id):
        raise MappingValidationError("mapping parse_id does not match workspace")
    sheet_map = mapping.get("sheets")
    if not isinstance(sheet_map, dict) or not sheet_map:
        raise MappingValidationError("mapping.sheets must be a non-empty object")
    available = {sheet["sheet_id"]: sheet for sheet in workbook.get("sheets", [])}
    by_name = {sheet["name"]: sheet for sheet in workbook.get("sheets", [])}
    normalized_sheets = {}
    for requested_sheet, config in sheet_map.items():
        if not isinstance(config, dict):
            raise MappingValidationError(f"sheet mapping must be object: {requested_sheet}")
        sheet = available.get(requested_sheet) or by_name.get(requested_sheet)
        if sheet is None:
            raise MappingValidationError(f"sheet not found in workspace: {requested_sheet}")
        fields = config.get("fields")
        if config.get("include", True) and (not isinstance(fields, dict) or not fields):
            raise MappingValidationError(f"included sheet has no fields: {requested_sheet}")
        normalized_fields = {}
        for raw_field_name, source in (fields or {}).items():
            field_name = _normalize_mapping_field_name(raw_field_name)
            if field_name not in FIELD_NAMES:
                raise MappingValidationError(
                    f"unsupported normalized field: {raw_field_name}. "
                    f"supported fields: {_field_names_hint()}"
                    + _alias_hint(raw_field_name)
                )
            if not isinstance(source, dict):
                raise MappingValidationError(f"field source must be object: {field_name}")
            has_column = "column" in source
            has_constant = "constant" in source
            if has_column == has_constant:
                raise MappingValidationError(f"field {field_name} requires exactly one of column/constant")
            normalized_source = dict(source)
            if has_column:
                normalized_source["column"] = str(source["column"]).strip().upper()
                normalized_source["column_index"] = _column_index(normalized_source["column"])
                if normalized_source["column_index"] > int(sheet.get("total_columns", 0) or 0):
                    raise MappingValidationError(
                        f"field {field_name} column {normalized_source['column']} exceeds sheet width"
                    )
            transforms = source.get("transform", [])
            if isinstance(transforms, str):
                transforms = [transforms]
            if isinstance(transforms, list):
                transforms = [TRANSFORM_ALIASES.get(str(item).lower(), item) for item in transforms]
            if not isinstance(transforms, list) or any(item not in ALLOWED_TRANSFORMS for item in transforms):
                raise MappingValidationError(
                    f"field {field_name} contains unsupported transform. "
                    f"supported transforms: {', '.join(sorted(ALLOWED_TRANSFORMS))}"
                )
            normalized_source["transform"] = transforms
            normalized_fields[field_name] = normalized_source
        start = int(config.get("data_start_row", 1) or 1)
        end = int(config.get("data_end_row", sheet.get("total_rows", 0)) or 0)
        if start < 1 or end < start or end > int(sheet.get("total_rows", 0) or 0):
            raise MappingValidationError(f"invalid data row range for {requested_sheet}: {start}-{end}")
        skip_rules = config.get("skip_rules") or {}
        if not isinstance(skip_rules, dict):
            raise MappingValidationError(f"skip_rules must be object: {requested_sheet}")
        empty_fields = skip_rules.get("skip_empty_fields", [])
        if isinstance(empty_fields, str):
            empty_fields = [empty_fields]
        if isinstance(empty_fields, list):
            empty_fields = [_normalize_mapping_field_name(field) for field in empty_fields]
        if not isinstance(empty_fields, list) or any(field not in normalized_fields for field in empty_fields):
            raise MappingValidationError(f"skip_empty_fields must reference mapped fields: {requested_sheet}")
        keywords = skip_rules.get("skip_keywords", [])
        if isinstance(keywords, str):
            keywords = [keywords]
        if not isinstance(keywords, list):
            raise MappingValidationError(f"skip_keywords must be list: {requested_sheet}")
        # D78 (2026-08-20): 宽表支持 — 每行 1-N 个 POD 价格组 (如德翔 Tier Guide).
        # pod_groups 为数组, 每组定义该 POD 组的价格列 (列字母):
        #   [{"pod": "G", "of_20": "H", "of_40": "I", "of_40hq": "J"}, ...]
        # 校验: 必须是 list; 每组列必须合法列字母且在 sheet 宽度内.
        pod_groups = config.get("pod_groups") or []
        normalized_pod_groups = []
        if pod_groups:
            if not isinstance(pod_groups, list):
                raise MappingValidationError(f"pod_groups must be list: {requested_sheet}")
            for gidx, group in enumerate(pod_groups):
                if not isinstance(group, dict):
                    raise MappingValidationError(f"pod_groups[{gidx}] must be object: {requested_sheet}")
                ng = {}
                for key in ("pod", "of_20", "of_40", "of_40hq", "of_20nor", "of_40nor", "of_45", "dg_20", "dg_40", "dg_40hq"):
                    col_raw = group.get(key)
                    if col_raw is None:
                        ng[key] = None
                        continue
                    col = str(col_raw).strip().upper()
                    if not _COLUMN_RE.fullmatch(col):
                        raise MappingValidationError(
                            f"pod_groups[{gidx}].{key} invalid Excel column: {col_raw!r} ({requested_sheet})"
                        )
                    if _column_index(col) > int(sheet.get("total_columns", 0) or 0):
                        raise MappingValidationError(
                            f"pod_groups[{gidx}].{key} column {col} exceeds sheet width ({requested_sheet})"
                        )
                    ng[key] = col
                normalized_pod_groups.append(ng)
        normalized_sheets[sheet["sheet_id"]] = {
            "sheet_id": sheet["sheet_id"],
            "sheet_name": sheet["name"],
            "include": bool(config.get("include", True)),
            "header_rows": [int(row) for row in config.get("header_rows", [])],
            "data_start_row": start,
            "data_end_row": end,
            "fields": normalized_fields,
            "pod_groups": normalized_pod_groups,
            "skip_rules": {
                "skip_empty_fields": empty_fields,
                "skip_all_price_empty": bool(skip_rules.get("skip_all_price_empty", False)),
                "skip_hidden_rows": bool(skip_rules.get("skip_hidden_rows", False)),
                "skip_keywords": [str(keyword) for keyword in keywords if str(keyword)],
            },
        }
    return {
        "schema_version": MAPPING_SCHEMA_VERSION,
        "parse_id": parse_id,
        "sheets": normalized_sheets,
    }


def _apply_transforms(value: Any, transforms: List[str]) -> Any:
    current = value
    for transform in transforms:
        if transform == "strip":
            current = "" if current is None else str(current).strip()
        elif transform == "upper":
            current = "" if current is None else str(current).upper()
        elif transform in ("number", "rate_number"):
            if current is None or (isinstance(current, str) and not current.strip()):
                current = None
                continue
            if isinstance(current, bool):
                raise ValueError("boolean is not a rate number")
            if isinstance(current, (int, float)):
                continue
            cleaned = str(current).strip().replace(",", "").replace("$", "")
            if transform == "rate_number":
                match = re.search(r"[-+]?\d+(?:\.\d+)?", cleaned)
                if not match:
                    raise ValueError(f"no numeric rate in {current!r}")
                cleaned = match.group(0)
            current = float(cleaned)
            if current.is_integer():
                current = int(current)
    return current


def _is_empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _detect_nor_only(entry: Dict[str, Any]) -> Tuple[bool, Dict[str, Any], Dict[str, Any]]:
    """v3.10.6.2 (D17): detect NOR-only rows and propose fillable map.

    A NOR-only row has:
      - standard prices ALL empty (of_20, of_40, of_40hq)
      - at least one NOR price present (of_20nor or of_40nor)

    Returns:
        (is_nor_only, fillable_map, source_values)
        - fillable_map: suggested {"of_20": 1500, "of_40": 2800} to merge
        - source_values: raw {"of_20nor": 1500, "of_40nor": 2800} for transparency
    """
    fillable: Dict[str, Any] = {}
    sources: Dict[str, Any] = {}
    for std_field, nor_field in (("of_20", "of_20nor"), ("of_40", "of_40nor")):
        nor_val = entry.get(nor_field)
        if not _is_empty(nor_val):
            try:
                fillable[std_field] = float(nor_val)
                sources[nor_field] = nor_val
            except (TypeError, ValueError):
                pass
    standard_empty = all(_is_empty(entry.get(f)) for f in STANDARD_PRICE_FIELDS)
    is_nor_only = standard_empty and bool(fillable)
    return is_nor_only, fillable, sources


def _draft_record_id(parse_id: str, sheet_id: str, row_number: int) -> str:
    token = hashlib.sha256(f"{parse_id}|{sheet_id}|{row_number}".encode("utf-8")).hexdigest()[:16]
    return f"draft_{token}"


def _row_values(row: Dict[str, Any]) -> Dict[int, Dict[str, Any]]:
    return {int(cell["column_index"]): cell for cell in row.get("cells", [])}


def _field_value(source: Dict[str, Any], cells: Dict[int, Dict[str, Any]]) -> Tuple[Any, Optional[str]]:
    if "constant" in source:
        return _apply_transforms(source.get("constant"), source.get("transform", [])), None
    cell = cells.get(int(source["column_index"])) or {}
    value = cell.get("display_value")
    if value is None:
        value = cell.get("raw")
    return _apply_transforms(value, source.get("transform", [])), cell.get("coordinate")


# OCR 条目专有字段 → 标准字段语义 (D74, 2026-08-20):
# line_date 开船日期 → etd; service 服务代码 (NCP/CPF/CV2S...) / terminal 码头 → 并入 remark
_OCR_REMARK_FIELDS = {"service", "terminal"}


def _normalize_ocr_entry(ocr_entry: Dict[str, Any]) -> Dict[str, Any]:
    """OCR 已归一化条目 → draft 条目字段 (D74: 无需 mapping, 直接透传 + 语义映射)."""
    normalized: Dict[str, Any] = {}
    remark_parts: List[str] = []
    for key, value in (ocr_entry or {}).items():
        if key.startswith("_"):
            continue
        if key in FIELD_NAMES:
            normalized[key] = value
        elif key == "line_date":
            normalized["etd"] = value
        elif key in _OCR_REMARK_FIELDS:
            remark_parts.append(f"{key}={value}")
        else:
            remark_parts.append(f"{key}={value}")
    if remark_parts:
        existing = str(normalized.get("remark") or "").strip()
        joined = " | ".join(remark_parts)
        normalized["remark"] = f"{existing} | {joined}" if existing else joined
    return normalized


def _append_entry(
    entries: List[Dict[str, Any]],
    p0_details: List[Dict[str, Any]],
    p1_details: List[Dict[str, Any]],
    p2_details: List[Dict[str, Any]],
    nor_only_records: List[Dict[str, Any]],
    field_missing_counter: Counter,
    normalized: Dict[str, Any],
    parse_id: str,
    sheet_id: str,
    row_number: int,
    provenance: Dict[str, Any],
) -> None:
    """把一条 normalized 数据追加进 draft entries, 并收集 P0/P1/P2 明细.

    Excel 与 OCR (D74) 两条路径共用: 分类校验 → draft_record_id →
    provenance/classification 注入 → 明细/nor_only/缺失字段统计.
    """
    # D80 (2026-08-28): carrier → 订舱口主数据自动匹配订舱代理中文名 (P0).
    # 匹配不到保持空 → classify_entry 报 P0 缺 → awaiting_user_fields → 问询业务.
    if not str(normalized.get("booking_agent") or "").strip():
        carrier = str(normalized.get("carrier") or "").strip()
        if carrier:
            try:
                from booking_agent_master import get_ba_master
                name, status = get_ba_master().resolve(carrier)
                if status == "ok" and name:
                    normalized["booking_agent"] = name
            except Exception:
                pass
    entry = NormalizedRateEntry.from_dict(normalized)
    classification = classify_entry(entry)
    draft_id = _draft_record_id(parse_id, sheet_id, row_number)
    normalized["draft_record_id"] = draft_id
    normalized["_provenance"] = provenance
    normalized["_classification"] = classification
    normalized["_draft_schema"] = DRAFT_SCHEMA_VERSION
    entries.append(normalized)
    detail = {
        "draft_record_id": draft_id,
        "source_row_id": provenance["source_row_id"],
        "missing": [],
    }
    if classification["p0_missing"]:
        detail["missing"] = classification["p0_missing"]
        p0_details.append(detail)
    if classification["p1_missing"]:
        p1_details.append({**detail, "missing": classification["p1_missing"]})
    if classification["p2_missing"]:
        p2_details.append({**detail, "missing": classification["p2_missing"]})
    # v3.10.6.2 (D17): detect NOR-only rows and propose fillable map.
    # Priority for LLM: list these in next reply + offer write-record --merge to fill.
    is_nor_only, fillable, sources = _detect_nor_only(normalized)
    if is_nor_only:
        nor_only_records.append({
            "draft_record_id": draft_id,
            "source_row_id": provenance["source_row_id"],
            "sheet_id": sheet_id,
            "sheet_name": provenance.get("sheet", sheet_id),
            "row_number": row_number,
            "fillable": fillable,
            "source_values": sources,
        })
    for missing in classification["p0_missing"] + classification["p1_missing"] + classification["p2_missing"]:
        field_missing_counter[missing] += 1


def build_draft(
    root: Optional[str],
    parse_id: str,
    mapping_value: Any,
    *,
    expected_revision: int,
) -> Dict[str, Any]:
    workspace = ParseWorkspace(root)
    loaded = workspace.load(parse_id)
    current_revision = int(loaded["state"].get("revision", 0))
    if int(expected_revision) != 0 and int(expected_revision) != current_revision:
        raise StaleWorkspaceError(int(expected_revision), current_revision)
    workspace_dir = Path(loaded["path"])
    workbook_path = workspace_dir / "raw" / "workbook.json"
    entries_source_path = workspace_dir / "raw" / "entries.json"

    entries: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    candidate_rows = 0
    source_rows = 0
    skip_counter = Counter()
    p0_details = []
    p1_details = []
    p2_details = []
    # v3.10.6.2 (D17): NOR-only records that LLM should优先修补
    nor_only_records: List[Dict[str, Any]] = []
    field_missing_counter = Counter()

    if workbook_path.exists():
        # Excel/CSV/PDF 提取路径: workbook.json + mapping → 行级归一化
        workbook = json.loads(workbook_path.read_text(encoding="utf-8"))
        mapping = validate_mapping(_load_json_or_value(mapping_value), workbook, parse_id)
        sheet_meta = {sheet["sheet_id"]: sheet for sheet in workbook.get("sheets", [])}
    else:
        # D74 (2026-08-20): OCR 路径 — raw/entries.json 已是归一化条目, 无 workbook.json.
        # 跳过 mapping, 直接以 OCR 条目为 draft 源, 走与 Excel 相同的校验/写库/状态链路,
        # 恢复 OCR 路径缺失的 draft 阶段可视化校验.
        if not entries_source_path.exists():
            raise MappingValidationError(
                f"workspace 缺少 raw/workbook.json 与 raw/entries.json, 无法 build_draft: {parse_id}"
            )
        ocr_entries = json.loads(entries_source_path.read_text(encoding="utf-8")).get("entries", []) or []
        source_rows = len(ocr_entries)
        mapping = {
            "schema_version": MAPPING_SCHEMA_VERSION,
            "parse_id": parse_id,
            "sheets": {},
            "source": "ocr-entries",
        }
        sheet_meta = {}
        for index, ocr_entry in enumerate(ocr_entries, 1):
            candidate_rows += 1
            normalized = _normalize_ocr_entry(ocr_entry)
            provenance = {
                "source_file": loaded["manifest"]["source_file"],
                "source_sha256": loaded["manifest"]["source_sha256"],
                "parser": "ocr-image",
                "sheet": "OCR",
                "sheet_id": "OCR",
                "row": index,
                "source_row_id": f"OCR!{index}",
                "cells": {},
                "constants": {},
            }
            _append_entry(
                entries, p0_details, p1_details, p2_details,
                nor_only_records, field_missing_counter,
                normalized, parse_id, "OCR", index, provenance,
            )
    for sheet_id, config in mapping["sheets"].items():
        if not config["include"]:
            continue
        raw_path = workspace_dir / sheet_meta[sheet_id]["raw_file"]
        for row in _load_jsonl(raw_path):
            source_rows += 1
            row_number = int(row.get("row_number", 0) or 0)
            if row_number < config["data_start_row"] or row_number > config["data_end_row"]:
                continue
            if row_number in config["header_rows"]:
                continue
            candidate_rows += 1
            cells = _row_values(row)
            pod_groups = config.get("pod_groups") or []
            # D78 (2026-08-20): 宽表行展开 — 一行 N 个 POD 价格组 → N 条记录.
            # 共享字段 (fields 中除 POD/价格组字段) 只映射一次, 每组取组列值后生成一条.
            _GROUP_FIELDS = {"pod", "of_20", "of_40", "of_40hq", "of_20nor", "of_40nor", "of_45", "dg_20", "dg_40", "dg_40hq"}
            shared_sources = {k: v for k, v in config["fields"].items() if k not in _GROUP_FIELDS} if pod_groups else config["fields"]

            def _map_fields(sources):
                mapped: Dict[str, Any] = {}
                cells_map: Dict[str, str] = {}
                constants_map: Dict[str, Dict[str, Any]] = {}
                errors = []
                for field_name, source in sources.items():
                    try:
                        value, coordinate = _field_value(source, cells)
                    except (TypeError, ValueError) as exc:
                        value, coordinate = None, None
                        errors.append(f"{field_name}: {exc}")
                    mapped[field_name] = value
                    if coordinate:
                        cells_map[field_name] = coordinate
                    elif "constant" in source:
                        constants_map[field_name] = {
                            "value": value,
                            "source": source.get("source", "mapping_constant"),
                        }
                return mapped, cells_map, constants_map, errors

            normalized, provenance_cells, provenance_constants, transform_errors = _map_fields(shared_sources)

            def _group_skipped(norm, rules):
                reasons = []
                for field_name in rules["skip_empty_fields"]:
                    if _is_empty(norm.get(field_name)):
                        reasons.append(f"empty:{field_name}")
                if rules["skip_all_price_empty"] and all(_is_empty(norm.get(f)) for f in PRICE_FIELDS):
                    reasons.append("all_price_empty")
                if rules["skip_hidden_rows"] and row.get("is_hidden"):
                    reasons.append("hidden_row")
                if rules["skip_keywords"]:
                    row_text = " ".join(str(cell.get("display") or "") for cell in row.get("cells", []))
                    for keyword in rules["skip_keywords"]:
                        if keyword in row_text:
                            reasons.append(f"keyword:{keyword}")
                if transform_errors:
                    reasons.extend(f"transform_error:{error}" for error in transform_errors)
                return reasons

            rules = config["skip_rules"]
            entry_provenance = {
                "source_file": loaded["manifest"]["source_file"],
                "source_sha256": loaded["manifest"]["source_sha256"],
                "parser": workbook.get("parser", "workbook-v2"),
                "sheet": config["sheet_name"],
                "sheet_id": sheet_id,
                "row": row_number,
                "source_row_id": row.get("source_row_id"),
                "cells": provenance_cells,
                "constants": provenance_constants,
            }

            if not pod_groups:
                reasons = _group_skipped(normalized, rules)
                if reasons:
                    unique_reasons = list(dict.fromkeys(reasons))
                    skipped.append({
                        "source_row_id": row.get("source_row_id"),
                        "sheet_id": sheet_id,
                        "sheet_name": config["sheet_name"],
                        "row_number": row_number,
                        "reasons": unique_reasons,
                    })
                    skip_counter.update(unique_reasons)
                    continue
                _append_entry(
                    entries, p0_details, p1_details, p2_details,
                    nor_only_records, field_missing_counter,
                    normalized, parse_id, sheet_id, row_number, entry_provenance,
                )
                continue

            # 宽表: 逐组展开
            for gidx, group in enumerate(pod_groups):
                gnorm = dict(normalized)
                gcells: Dict[str, str] = {}
                gconsts: Dict[str, Dict[str, Any]] = {}
                for key, col in group.items():
                    if col is None:
                        continue
                    gcell = cells.get(_column_index(col)) or {}
                    gvalue = gcell.get("display_value")
                    if gvalue is None:
                        gvalue = gcell.get("raw")
                    gnorm[key] = gvalue
                    if gcell.get("coordinate"):
                        gcells[key] = gcell["coordinate"]
                # 组内 POD 为空 → 该组无记录意义, 强制 skip (不产生空 POD 条目)
                if _is_empty(gnorm.get("pod")):
                    skipped.append({
                        "source_row_id": row.get("source_row_id"),
                        "sheet_id": sheet_id,
                        "sheet_name": config["sheet_name"],
                        "row_number": row_number,
                        "pod_group": gidx,
                        "reasons": ["empty:pod"],
                    })
                    skip_counter.update(["empty:pod"])
                    continue
                greasons = _group_skipped(gnorm, rules)
                if greasons:
                    unique_reasons = list(dict.fromkeys(greasons))
                    skipped.append({
                        "source_row_id": row.get("source_row_id"),
                        "sheet_id": sheet_id,
                        "sheet_name": config["sheet_name"],
                        "row_number": row_number,
                        "pod_group": gidx,
                        "reasons": unique_reasons,
                    })
                    skip_counter.update(unique_reasons)
                    continue
                gprovenance = dict(entry_provenance)
                gprovenance["row"] = row_number
                gprovenance["pod_group"] = gidx
                gprovenance["cells"] = {**provenance_cells, **gcells}
                _append_entry(
                    entries, p0_details, p1_details, p2_details,
                    nor_only_records, field_missing_counter,
                    gnorm, parse_id, sheet_id, row_number * 100 + gidx, gprovenance,
                )

    summary = {
        "schema_version": "rate-draft-summary/v1",
        "parse_id": parse_id,
        "source_rows": source_rows,
        "candidate_rows": candidate_rows,
        "valid_entries": len(entries),
        "skipped_rows": len(skipped),
        "skip_reasons": dict(sorted(skip_counter.items())),
        "p0_missing_records": len(p0_details),
        "p1_missing_records": len(p1_details),
        "p2_warning_records": len(p2_details),
        "missing_field_counts": dict(sorted(field_missing_counter.items())),
        "p0_details": p0_details,
        "p1_details": p1_details,
        "p2_details": p2_details,
        "nor_only_count": len(nor_only_records),
        "nor_only_records": nor_only_records,
    }

    workspace.write_json(parse_id, "mapping/mapping.json", mapping)
    _atomic_write_jsonl(workspace_dir / "draft" / "entries.jsonl", entries)
    _atomic_write_jsonl(workspace_dir / "draft" / "skipped-rows.jsonl", skipped)
    workspace.write_json(parse_id, "draft/missing-fields.json", summary)
    next_status = "awaiting_user_fields" if p0_details else "draft_ready"
    state = workspace.update_state(
        parse_id,
        expected_revision=expected_revision,
        status=next_status,
        phase="draft",
        last_action="draft_built",
        next_action="collect_missing_fields" if p0_details else "review_draft",
        updates={
            "draft_summary": {
                key: summary[key]
                for key in (
                    "source_rows", "candidate_rows", "valid_entries", "skipped_rows",
                    "p0_missing_records", "p1_missing_records", "p2_warning_records",
                )
            }
        },
    )
    d29_warnings = []
    if summary.get("valid_entries", 0) > 0:
        valid_count = int(summary["valid_entries"])
        missing_counts = summary.get("missing_field_counts", {}) or {}
        core_ship_fields = {
            "船名 (Vessel Name)": "vessel",
            "航次 (Voyage)": "voyage",
            "ETD": "etd",
            "航程(天)": "tt_days",
            "直航": "direct",
            "班期 (Frequency)": "frequency",
        }
        for label, field_name in core_ship_fields.items():
            missed = int(missing_counts.get(label, 0) or 0)
            if missed >= valid_count:
                d29_warnings.append(
                    f"D29: {missed}/{valid_count} 条记录全部缺「{label}」({field_name}). "
                    f"请确认 Excel 中是否有该列, 如果有, 请在 mapping_json 里补映射 {field_name} 字段后重新 build_draft."
                )
    return {
        "code": "DRAFT_BUILT",
        "parse_id": parse_id,
        "revision": state["revision"],
        "state": state["status"],
        "next_action": state["next_action"],
        **{key: summary[key] for key in (
            "source_rows", "candidate_rows", "valid_entries", "skipped_rows",
            "skip_reasons", "p0_missing_records", "p1_missing_records",
            "p2_warning_records", "missing_field_counts", "nor_only_count",
        )},
        "nor_only_records": summary["nor_only_records"],
        "d29_warnings": d29_warnings,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--parse-id", required=True)
    parser.add_argument("--mapping", default="{}", help="@file or JSON file path (OCR 路径忽略)")
    parser.add_argument("--expected-revision", required=True, type=int)
    parser.add_argument("--workspace-root", default=None)
    args = parser.parse_args()
    try:
        result = build_draft(
            args.workspace_root,
            args.parse_id,
            args.mapping,
            expected_revision=args.expected_revision,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except StaleWorkspaceError as exc:
        print(json.dumps({
            "code": exc.code,
            "success": False,
            "expected_revision": exc.expected_revision,
            "current_revision": exc.current_revision,
            "error": str(exc),
        }, ensure_ascii=False, indent=2))
        return 3
    except Exception as exc:
        print(json.dumps({
            "code": getattr(exc, "code", "DRAFT_BUILD_ERROR"),
            "success": False,
            "error": str(exc),
        }, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    sys.exit(main())
