# -*- coding: utf-8 -*-
"""
运价文件解析结果缓存 — 容器内持久化
- 同一文件（hash 命中）不重解析，直接复用
- 保存内容：源文件路径 + OCR 原始 markdown + parse_file 输出 entries
- 路径：/home/node/.openclaw/workspace/scratch/rate-parses/<hash>.json
  （绑定到 NAS：/vol2/1000/dockerapps/openclaw/claw1/workspace/scratch/）

使用：
  from parse_cache import ParseCache
  cache = ParseCache()
  cached = cache.get(file_path)   # 命中返回 dict，未命中返回 None
  cache.set(file_path, parse_result_dict)
"""
import hashlib
import json
import os
import time
from typing import Optional, Dict, Any

PARSER_CACHE_VERSION = "20260716-v6"

CACHE_ROOT = os.environ.get(
    "DG_RATE_CACHE_DIR",
    "/home/node/.openclaw/workspace/scratch/rate-parses",
)


def _md5_file(path: str) -> str:
    """文件内容 MD5。"""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _md5_str(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


class ParseCache:
    def __init__(self, root: str = None):
        self.root = root or CACHE_ROOT
        os.makedirs(self.root, exist_ok=True)

    def _key(self, file_path: str) -> str:
        """生成缓存 key：MD5(绝对路径 + 文件大小 + 修改时间)。"""
        if not os.path.isfile(file_path):
            return _md5_str(file_path + str(time.time()))
        st = os.stat(file_path)
        sig = "|".join([
            PARSER_CACHE_VERSION,
            os.path.abspath(file_path),
            str(st.st_size),
            str(int(st.st_mtime)),
        ])
        return _md5_str(sig)

    def get(self, file_path: str) -> Optional[Dict[str, Any]]:
        """命中返回缓存 dict，未命中返回 None。"""
        key = self._key(file_path)
        p = os.path.join(self.root, key + ".json")
        if not os.path.isfile(p):
            return None
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def set(self, file_path: str, payload: Dict[str, Any]) -> str:
        """写入缓存，返回 key。"""
        key = self._key(file_path)
        payload = dict(payload)
        payload["_cache"] = {
            "key": key,
            "src_path": os.path.abspath(file_path),
            "src_size": os.path.getsize(file_path) if os.path.isfile(file_path) else 0,
            "src_mtime": int(os.path.getmtime(file_path)) if os.path.isfile(file_path) else 0,
            "cached_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        p = os.path.join(self.root, key + ".json")
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
        return key

    def list_recent(self, limit: int = 20) -> list:
        """列出最近 N 个缓存。"""
        if not os.path.isdir(self.root):
            return []
        files = []
        for fn in os.listdir(self.root):
            if not fn.endswith(".json"):
                continue
            p = os.path.join(self.root, fn)
            try:
                st = os.stat(p)
                files.append((st.st_mtime, p))
            except Exception:
                continue
        files.sort(reverse=True)
        out = []
        for _, p in files[:limit]:
            try:
                with open(p, "r", encoding="utf-8") as f:
                    d = json.load(f)
                out.append({
                    "key": d.get("_cache", {}).get("key", ""),
                    "src_path": d.get("_cache", {}).get("src_path", ""),
                    "cached_at": d.get("_cache", {}).get("cached_at", ""),
                    "entry_count": (d.get("parse_result", {}) or {}).get("entry_count", 0),
                })
            except Exception:
                continue
        return out


def _main():
    import argparse
    ap = argparse.ArgumentParser(description="运价解析缓存")
    ap.add_argument("--list", action="store_true", help="列最近缓存")
    ap.add_argument("--root", help="缓存根目录")
    args = ap.parse_args()
    c = ParseCache(args.root)
    if args.list:
        for r in c.list_recent(20):
            print(json.dumps(r, ensure_ascii=False))
    else:
        print("Cache root:", c.root)
        print("Recent:")
        for r in c.list_recent(5):
            print(" ", r)


if __name__ == "__main__":
    _main()