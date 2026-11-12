# -*- coding: utf-8 -*-
"""
飞书多维表格字段辅助工具

v3.10.6.2 (P0 沃行对齐) — 重大变更:
  - ensure_options / add_options 已停用, 不再自动追加 select 字段选项.
  - 原因: 船公司字段从 select 改为非 select, 取值完全靠 booking_agent_master.json
    字典匹配 (飞书侧 schema 改造由任务 3 协调).
  - API 表面保留仅为兼容调用方 (lark_rate_writer._enforce_select_options 仍会调用),
    所有 want_options 一律标记 skipped 并返回禁用警告.

旧用途 (v3.10.6.2 之前):
  - 解析运价时遇到新船公司（SNL、IAL 等）→ 自动追加到「船公司」字段选项
  - 避免每次都要人工先在飞书 UI 加选项再让可可写记录
  - 错误透明：返回 existing/added/skipped 三类清单

使用方式 (兼容调用, 不再自动加选项):
  from lark_field_helper import LarkFieldHelper

  helper = LarkFieldHelper()
  # 1) 拉取某字段当前所有选项 (只读, 不变)
  opts = helper.list_options(base_token, table_id, "船公司")
  # 2) ensure_options 已禁用: 返回 {"added": [], "skipped": [...], "warnings": [...]}
  result = helper.ensure_options(base_token, table_id, "船公司", ["SNL", "IAL"])
  # result["added"] 永远为空, result["skipped"] = ["SNL", "IAL"]

底层命令 (通过 paramiko SSH 到 NAS 调用容器内 lark-cli, 仅只读 list/field-list):
  lark base +field-list  --base-token X --table-id Y
  lark base +field-update --base-token X --table-id Y --field-id Z --json '{...}'  # 已停用
"""
import json
import os
from typing import List, Dict, Any, Optional

try:
    import paramiko
    HAS_PARAMIKO = True
except ImportError:
    HAS_PARAMIKO = False


DEFAULT_CONFIG = {
    "nas_host": "192.168.31.128",
    "nas_port": int(os.environ.get("NAS_SSH_PORT", "2122")),
    "nas_user": "admin",
    "nas_password": "Zengs_19761029",
    "container_name": "Openclaw-coco",
}


class LarkFieldHelper:
    def __init__(self, config: Dict[str, Any] = None):
        self.config = {**DEFAULT_CONFIG, **(config or {})}
        if not HAS_PARAMIKO:
            raise ImportError("paramiko 未安装")

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

    # ---------- field-list ----------
    def list_fields(self, base_token: str, table_id: str) -> List[dict]:
        """获取表中所有字段（包含 id、name、type、property.options）。"""
        cmd = (
            f"sudo docker exec {self.config['container_name']} lark-cli "
            f"base +field-list --base-token {base_token} --table-id {table_id} 2>&1"
        )
        out = self._exec(cmd)
        try:
            data = json.loads(out)
        except Exception as e:
            raise RuntimeError(f"field-list 返回非 JSON: {out[:300]}; err={e}")
        if data.get("ok") is False:
            raise RuntimeError("field-list 失败: " + json.dumps(data, ensure_ascii=False)[:300])
        items = data.get("data", {}).get("fields", []) or data.get("data", {}).get("items", [])
        return items

    def find_field(self, base_token: str, table_id: str, field_name: str) -> Optional[dict]:
        for f in self.list_fields(base_token, table_id):
            if (f.get("name") == field_name or f.get("field_name") == field_name or
                f.get("id") == field_name or f.get("field_id") == field_name):
                return f
        return None

    def list_options(self, base_token: str, table_id: str, field_name: str) -> List[str]:
        fld = self.find_field(base_token, table_id, field_name)
        if not fld:
            raise RuntimeError(f"字段 {field_name!r} 不存在")
        prop = fld.get("property", {}) or {}
        opts = prop.get("options", []) or fld.get("options", []) or []
        result = []
        for o in opts:
            if isinstance(o, dict):
                nm = o.get("name", "") or o.get("text", "")
                if nm:
                    result.append(nm)
            elif isinstance(o, str):
                result.append(o)
        return result

    # v3.10.6.2: 沃行 P0 — 船公司字段改为非 select, 不再自动追加 carrier 选项.
    # carrier 取值完全靠 booking_agent_master.json 字典匹配 (飞书侧 schema 改造由任务 3 协调).
    # 保留 add_options 方法签名但禁用, 避免破坏 lark_rate_writer._enforce_select_options 调用方.
    def add_options(self, base_token: str, table_id: str, field_id: str,
                    new_options: List[str], existing_options: List[str] = None) -> dict:
        """v3.10.6.2: 自动加选项已停用 — 沃行不再用 select 字段承载 carrier.

        保留方法签名仅为向后兼容 (lark_rate_writer._enforce_select_options 仍会调用),
        所有 new_options 一律标记 skipped 并返回禁用警告.
        """
        return {
            "added": [],
            "skipped": list(new_options),
            "msg": "v3.10.6.2 自动加选项已停用 (沃行 P0): carrier 字段改为非 select, "
                   "取值完全靠 booking_agent_master.json 字典匹配",
        }

    def ensure_options(self, base_token: str, table_id: str,
                       field_name: str, want_options: List[str]) -> dict:
        """v3.10.6.2: 已停用自动加选项, 仅保留 API 表面 (兼容 lark_rate_writer 调用).

        所有 want_options 一律标记 skipped 并返回禁用警告; 不再调用 add_options.
        Returns dict (结构同旧版本, 但 added 永远为空):
          {
            "field_id": "",
            "field_name": ...,
            "field_type": "",
            "existing": [],
            "added": [],        # 永远空
            "skipped": [...],   # 全部 want_options
            "warnings": [...],  # 禁用警告
          }
        """
        result = {
            "field_id": "",
            "field_name": field_name,
            "field_type": "",
            "existing": [],
            "added": [],
            "skipped": list(want_options),
            "warnings": [
                "v3.10.6.2 自动加选项已停用 (沃行 P0): "
                "carrier 字段改为非 select, 取值完全靠 booking_agent_master.json 字典匹配"
            ],
        }
        return result


def _main():
    import argparse
    ap = argparse.ArgumentParser(description="飞书多维表格字段选项辅助")
    ap.add_argument("--base-token", required=True)
    ap.add_argument("--table-id", required=True)
    ap.add_argument("--field", required=True, help="字段名，如 '船公司'")
    ap.add_argument("--add", nargs="*", help="要追加的选项（缺省只列出现有选项）")
    args = ap.parse_args()
    h = LarkFieldHelper()
    if args.add:
        result = h.ensure_options(args.base_token, args.table_id, args.field, args.add)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        opts = h.list_options(args.base_token, args.table_id, args.field)
        print(json.dumps({"field": args.field, "options": opts}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _main()