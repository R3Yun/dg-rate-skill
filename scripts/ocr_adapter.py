# -*- coding: utf-8 -*-
"""OCR adapter: image/PDF -> mineru-open-api -> Markdown -> parse_file.py

Supported inputs:
  - .png .jpg .jpeg .webp .bmp .tiff   via flash-extract / extract
  - .pdf                               via flash-extract / extract
  - .xlsx .xls .docx .pptx             via flash-extract (no-token limit 10M/20p)

Modes:
  - default: flash-extract (no token required, instant)
  - with MINERU_TOKEN env or --accuracy precision: use extract (higher quality)

Output cached at /tmp/ocr_cache/ to avoid re-OCR on same file.

CLI:
  python ocr_adapter.py <file> [--accuracy flash|precision|auto] [--lang ch|en]
"""
import argparse
import hashlib
import os
import subprocess
import sys
from pathlib import Path

CACHE_DIR = Path("/tmp/ocr_cache")

OCR_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tiff", ".tif", ".pdf"}

MINERU_BIN = "mineru-open-api"


def needs_ocr(path):
    """Only image/PDF need OCR; other formats handled by parse_file.py."""
    return Path(path).suffix.lower() in OCR_EXTS


def _md5(s):
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def _cache_path(src, accuracy, lang):
    h = _md5(src + "|" + accuracy + "|" + lang + "|" + str(os.path.getsize(src)))
    return CACHE_DIR / (h + "_" + Path(src).stem + "_" + accuracy + "_" + lang + ".md")


def _run(cmd, timeout=300):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(
            MINERU_BIN + " failed: rc=" + str(r.returncode) +
            " stderr=" + r.stderr.strip()[:300] +
            " stdout=" + r.stdout.strip()[:300]
        )
    return r.stdout, r.stderr


def run_ocr(src, accuracy="auto", lang="ch", keep_cache=True, timeout=300):
    """Returns dict {code, source, accuracy, lang, md, cache, md_chars}."""
    if not Path(src).is_file():
        raise FileNotFoundError(src)

    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    if accuracy == "auto":
        have_token = bool(os.environ.get("MINERU_TOKEN", "").strip())
        accuracy = "precision" if have_token else "flash"

    cache = _cache_path(src, accuracy, lang)
    if keep_cache and cache.exists() and cache.stat().st_size > 0:
        md = cache.read_text(encoding="utf-8")
        return {
            "code": "ok", "source": "cache", "accuracy": accuracy, "lang": lang,
            "md": md, "cache": str(cache), "md_chars": len(md),
        }

    cmd = [MINERU_BIN]
    cmd.append("extract" if accuracy == "precision" else "flash-extract")
    cmd += [src, "--language", lang, "--ocr", "--table"]

    stdout, _ = _run(cmd, timeout=timeout)
    md = stdout.strip()
    if not md:
        raise RuntimeError(MINERU_BIN + " returned empty markdown")

    md = _html_table_to_markdown(md)

    cache.write_text(md, encoding="utf-8")
    return {
        "code": "ok", "source": "api", "accuracy": accuracy, "lang": lang,
        "md": md, "cache": str(cache), "md_chars": len(md),
    }




def _html_table_to_markdown(html_md):
    """把 mineru flash-extract 的 <table>...</table> 转成 markdown 表格。
    支持 colspan/rowspan、空 td、混合多 table。"""
    import re
    if "<table" not in html_md:
        return html_md

    def cell_text(s):
        s = re.sub(r"<[^>]+>", "", s)
        s = (s.replace("&nbsp;", " ")
              .replace("&amp;", "&")
              .replace("&lt;", "<")
              .replace("&gt;", ">")
              .replace("&quot;", '"'))
        return s.strip()

    out = []
    last = 0
    for m in re.finditer(r"<table[^>]*>(.*?)</table>", html_md, re.DOTALL):
        out.append(html_md[last:m.start()].strip())
        block = m.group(1)
        rows = []
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", block, re.DOTALL):
            tds = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.DOTALL)
            if not tds:
                continue
            cells = [cell_text(t) for t in tds]
            rows.append(cells)
        if not rows:
            last = m.end()
            continue
        ncols = max(len(r) for r in rows)
        rows = [r + [""] * (ncols - len(r)) for r in rows]
        header = "| " + " | ".join(rows[0]) + " |"
        sep = "| " + " | ".join("---" for _ in range(ncols)) + " |"
        body_lines = []
        for r in rows[1:]:
            body_lines.append("| " + " | ".join(r) + " |")
        body = "\n".join(body_lines)
        if body:
            out.append("\n".join([header, sep, body]))
        else:
            out.append("\n".join([header, sep]))
        out.append("")
        last = m.end()
    out.append(html_md[last:].strip())
    cleaned_parts = [s for s in out if s]
    return "\n\n".join(cleaned_parts)

def to_temp_markdown(src, **kwargs):
    """Run OCR on image/PDF, return path of cached Markdown."""
    res = run_ocr(src, **kwargs)
    return res["cache"]


def main():
    ap = argparse.ArgumentParser(description="OCR adapter (image/PDF -> Markdown)")
    ap.add_argument("path", help="image or pdf file path")
    ap.add_argument("--accuracy", choices=["auto", "flash", "precision"], default="auto")
    ap.add_argument("--lang", default="ch", choices=["ch", "en"])
    ap.add_argument("--no-cache", dest="keep_cache", action="store_false")
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--pretty", action="store_true")
    args = ap.parse_args()

    try:
        import json as _json
        res = run_ocr(args.path, accuracy=args.accuracy, lang=args.lang,
                       keep_cache=args.keep_cache, timeout=args.timeout)
        pretty = res.copy()
        if len(pretty.get("md", "")) > 4000:
            pretty["md_preview"] = pretty["md"][:4000] + "\n... [" + str(len(pretty["md"])) + " chars total, see full at " + pretty["cache"] + "]"
            pretty.pop("md", None)
        print(_json.dumps(pretty, ensure_ascii=False,
                          indent=2 if args.pretty else None))
    except Exception as e:
        sys.stderr.write("[ERROR] " + type(e).__name__ + ": " + str(e) + "\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
