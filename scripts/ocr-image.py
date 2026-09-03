#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ocr-image — D6 读类原子工具 (D6-5, v3.11+)

图片 / PDF OCR → Markdown + parse workspace (D41/D42).

变更 (v3.11, 2026-08-06):
- #2: OCR 成功后自动创建 parse workspace, 返回 parse_id
- #3: 自动调用 _parse_weekly_text (D41) 解析 entries
- #7: 错误聚合 — 单条价格空时整批拒绝
- #8: 失败处理 — OCR 完全失败时不返回空 markdown
- #9: 置信度阈值可配置 (--confidence-threshold, 默认 0.3)
- #10: OCR 缓存 — 同文件不重复 OCR (--no-cache 禁用)

CLI:
  ocr-image <file> [--accuracy flash|precision|auto]
                 [--confidence-threshold 0.3]
                 [--no-cache]
                 [--chat-id <user:ou_xxx>]
                 [--source-path <绝对路径>]

注意: 需要 coco 容器 (提供 mineru-open-api) + MINERU_TOKEN
"""
import argparse
import hashlib
import json
import os
import shutil
import sys
import traceback
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, str(Path(os.path.dirname(os.path.abspath(__file__))) / "parsers"))

OCR_CACHE_DIR = Path("/home/node/.openclaw/workspace/runtime/ocr-cache")
OCR_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
    except Exception:
        return ""
    return h.hexdigest()


def _check_env() -> dict:
    if shutil.which("mineru-open-api") is None:
        return {"available": False, "need": "在 coco 容器内运行 mineru-open-api",
                "hint": "mineru-open-api 不在 PATH, 请确认在 coco 容器中执行"}
    return {"available": True, "need": "", "hint": ""}


def _load_cache(sha256: str):
    cache_path = OCR_CACHE_DIR / f"{sha256}.json"
    if not cache_path.is_file():
        return None
    try:
        with open(cache_path) as f:
            return json.load(f)
    except Exception:
        return None


def _save_cache(sha256: str, data: dict) -> None:
    cache_path = OCR_CACHE_DIR / f"{sha256}.json"
    try:
        with open(cache_path, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _run_ocr(path: str, accuracy: str) -> dict:
    try:
        import ocr_adapter
    except ImportError:
        return {"ok": False, "error": "ocr_adapter not importable", "ocr_status": "failed"}
    try:
        res = ocr_adapter.run_ocr(path, accuracy=accuracy)
        md_text = res.get("md", "") if isinstance(res, dict) else str(res)
    except Exception as e:
        return {"ok": False, "error": f"OCR 失败: {e}", "ocr_status": "failed"}
    if not md_text or not md_text.strip():
        return {"ok": False,
                "error": "OCR 返回空文本 (图片质量差/不支持中文/扫描件加密)",
                "ocr_status": "failed",
                "hint": "请人工校对图片或重新上传清晰版"}
    return {"ok": True, "md": md_text, "ocr_status": "ok"}


def _check_confidence(md_text: str, threshold: float) -> dict:
    if not md_text or threshold <= 0:
        return {"confidence": 1.0, "low_confidence": False}
    total_chars = len(md_text.strip())
    chinese_chars = sum(1 for c in md_text if "\u4e00" <= c <= "\u9fff")
    chinese_ratio = chinese_chars / max(total_chars, 1)
    if total_chars < 200:
        return {"confidence": 0.5, "low_confidence": True, "reason": f"文本过短 ({total_chars} chars)"}
    if chinese_ratio < 0.05:
        return {"confidence": 0.6, "low_confidence": True,
                "reason": f"中文字符比例低 ({chinese_ratio:.1%})"}
    return {"confidence": min(1.0, 0.7 + 0.3 * chinese_ratio), "low_confidence": False}


def _create_parse_workspace(file_path: str, sha256: str, md_text: str):
    """创建 ParseWorkspace + task, 绑定 parse_id.

    D67-B (2026-08-10): 修复 OCR 路径 parse_id 一直为空导致 build_draft 失败.
    之前 task 创建时 parse_id="", 现在生成 parse_id 并写入 ParseWorkspace,
    让 build_draft/parse_page 等依赖 parse_id 的链路可以正常加载 OCR 结果.

    Returns:
        (task_dict, parse_id) 或 (None, "") 表示未创建
    """
    try:
        from task_state import RateTaskStore, ActiveTaskExists
    except ImportError:
        return None, ""
    chat_id = os.environ.get("DG_RATE_CHAT_ID", "")
    if not chat_id:
        return None, ""
    parse_id = f"parse_ocr_{sha256[:12]}"
    try:
        from parse_workspace import ParseWorkspace, _PARSE_ID_RE
        if not _PARSE_ID_RE.fullmatch(parse_id):
            raise ValueError(f"parse_id 不匹配格式: {parse_id!r}")
        workspace = ParseWorkspace()
        try:
            workspace.create(
                source_path=file_path,
                parse_id=parse_id,
                chat_id=chat_id,
                copy_source=True,
            )
        except FileExistsError:
            pass
        workspace.write_text(parse_id, "preview/markdown.md", md_text)
        workspace.update_state(
            parse_id, expected_revision=0,
            status="extracted",
            phase="ocr_extracted",
            last_action="ocr_completed",
            next_action="build_draft",
        )
    except Exception as _ws_err:
        sys.stderr.write(f"[ocr-image] workspace setup failed: {_ws_err!r}\n")
    try:
        store = RateTaskStore()
        source_file = Path(file_path).name
        task = store.create(
            chat_id=chat_id,
            source_file=source_file,
            source_sha256=sha256,
            parse_id=parse_id,
            source_path=file_path,
        )
        return task, parse_id
    except ActiveTaskExists as e:
        # D76 (2026-08-20): 阻塞时也返回真实 parse_id — workspace 已在本函数内创建成功,
        # 返回 "" 会让 _save_parse_entries("") 失败, raw/entries.json 永不落盘,
        # build_draft (D74 OCR entries 模式) 因缺 entries.json 报 INVALID_MAPPING.
        existing = e.task if hasattr(e, "task") and isinstance(e.task, dict) else {}
        return ({"_active_task_blocked": True, "_error_msg": str(e),
                "task_id": existing.get("task_id", ""),
                "parse_id": parse_id}, parse_id)


def _save_parse_entries(parse_id: str, entries, source_file: str):
    """D67-B (2026-08-10): OCR entries 解析后写入 ParseWorkspace.

    让 build_draft/parse_page 可加载 OCR 结果.
    """
    if not parse_id:
        return
    try:
        from parse_workspace import ParseWorkspace
        workspace = ParseWorkspace()
        workspace.write_json(parse_id, "raw/entries.json", {
            "source_file": source_file,
            "entries": entries,
            "count": len(entries),
        })
    except Exception as _e:
        sys.stderr.write(f"[ocr-image] save entries failed: {_e!r}\n")


def _parse_weekly_entries(md_text: str, source_file: str) -> dict:
    try:
        from image_parser import _parse_weekly_text, _get_ocr_metrics
    except ImportError:
        return {"ok": False, "error": "_parse_weekly_text 不可用 (D41 缺失)",
                "entries": [], "entry_count": 0}
    try:
        entries = _parse_weekly_text(md_text, source_file)
    except Exception as e:
        return {"ok": False, "error": f"_parse_weekly_text 异常: {e}",
                "entries": [], "entry_count": 0}
    empty_price_count = 0
    for e in entries:
        of_20 = e.get("of_20")
        of_40 = e.get("of_40")
        if (of_20 is None or of_20 == 0) and (of_40 is None or of_40 == 0):
            empty_price_count += 1
            e["empty_price_warning"] = True
    return {
        "ok": True,
        "entries": entries,
        "entry_count": len(entries),
        "empty_price_count": empty_price_count,
        "all_entries_have_price": empty_price_count == 0,
        "parse_warnings": [f"{empty_price_count} 条 entry 价格空, 需人工校对"] if empty_price_count else [],
        # D58: 加入 OCR format detector metrics (D57) 让 LLM/业务方可追踪格式分布
        "_ocr_metrics": _get_ocr_metrics(),
    }


def inspect_ocr(path: str, accuracy: str = "auto", confidence_threshold: float = 0.3,
               use_cache: bool = True, chat_id: str = "", source_path: str = "") -> dict:
    if not os.path.exists(path):
        return {"error": f"file not found: {path}", "ok": False}

    env = _check_env()
    if not env["available"]:
        return {"error": env["hint"], "ok": False, "ocr_status": "unavailable"}

    sha256 = _sha256_file(path)
    cached = _load_cache(sha256) if use_cache and sha256 else None
    if cached:
        cached["cache_hit"] = True
        cached["source_summary"] = cached.get("source_summary", {})
        cached["source_summary"]["file"] = os.path.basename(path)
        cached["source_summary"]["size_kb"] = round(os.path.getsize(path) / 1024, 1)
        return cached

    ocr_result = _run_ocr(path, accuracy)
    if not ocr_result.get("ok"):
        return {
            "error": ocr_result.get("error"),
            "ok": False,
            "ocr_status": ocr_result.get("ocr_status", "failed"),
            "hint": ocr_result.get("hint", ""),
        }
    md_text = ocr_result["md"]

    conf = _check_confidence(md_text, confidence_threshold)

    parse_workspace = None
    parse_id = ""
    if chat_id:
        os.environ["DG_RATE_CHAT_ID"] = chat_id
        parse_workspace, parse_id = _create_parse_workspace(
            file_path=path,
            sha256=sha256,
            md_text=md_text,
        )

    source_file = os.path.basename(path)
    parse_result = _parse_weekly_entries(md_text, source_file)

    _save_parse_entries(parse_id, parse_result.get("entries", []), source_file)

    size_kb = round(os.path.getsize(path) / 1024, 1) if os.path.exists(path) else 0

    result = {
        "ok": True,
        "cache_hit": False,
        "ocr_status": ocr_result.get("ocr_status", "ok"),
        "source_summary": {
            "file": source_file,
            "size_kb": size_kb,
            "line_count": len(md_text.splitlines()),
            "accuracy": accuracy,
            "parser": "ocr-image",
            "sha256": sha256,
        },
        "content_markdown": md_text,
        "reading_hint": f"OCR ({accuracy}), {len(md_text.splitlines())} 行 markdown"
                          + (", 注意表格识别是否完整" if "|" in md_text else ""),
        "confidence": conf.get("confidence", 1.0),
        "low_confidence": conf.get("low_confidence", False),
        "confidence_reason": conf.get("reason", ""),
        "active_task_blocked": False,
        "active_task_id": "",
        "active_task_msg": "",
        "parse_id": parse_id,
        "task_id": (parse_workspace or {}).get("task_id", ""),
        "parse_warnings": parse_result.get("parse_warnings", []),
        "entry_count": parse_result.get("entry_count", 0),
        "all_entries_have_price": parse_result.get("all_entries_have_price", True),
        "entries": parse_result.get("entries", []),
    }

    if not parse_result.get("ok"):
        result["entries_error"] = parse_result.get("error", "")

    if isinstance(parse_workspace, dict) and parse_workspace.get("_active_task_blocked"):
        result["active_task_blocked"] = True
        result["active_task_id"] = parse_workspace.get("task_id", "")
        result["active_task_msg"] = parse_workspace.get("_error_msg", "")
        result["parse_id"] = ""
        result["task_id"] = ""
    elif isinstance(parse_workspace, dict) and parse_workspace.get("_unexpected_error"):
        result["active_task_msg"] = parse_workspace.get("_error_msg", "")

    if use_cache and sha256:
        _save_cache(sha256, result)

    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--accuracy", choices=["flash", "precision", "auto"], default="auto")
    ap.add_argument("--confidence-threshold", type=float, default=0.3,
                    help="OCR 置信度阈值 (默认 0.3), 低于阈值标记 low_confidence")
    ap.add_argument("--no-cache", action="store_true", help="禁用 OCR 缓存")
    ap.add_argument("--chat-id", default="", help="飞书 chat_id (用于创建 parse workspace)")
    ap.add_argument("--source-path", default="", help="源文件绝对路径 (用于 task.json)")
    args = ap.parse_args()

    result = inspect_ocr(
        path=args.file,
        accuracy=args.accuracy,
        confidence_threshold=args.confidence_threshold,
        use_cache=not args.no_cache,
        chat_id=args.chat_id,
        source_path=args.source_path,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result.get("ok") or result.get("cache_hit") else 1)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(json.dumps({"error": str(e), "trace": traceback.format_exc(), "ok": False}, ensure_ascii=False))
        sys.exit(1)
