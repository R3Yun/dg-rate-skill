# -*- coding: utf-8 -*-
"""
飞书多维表格字段辅助工具
用于在写记录前自动给单选/多选字段补齐选项

解决问题：
  1. 解析运价时遇到新船公司（SNL、IAL 等）→ 自动追加到「船公司」字段选项
  2. 避免每次都要人工先在飞书 UI 加选项再让可可写记录
  3. 错误透明：返回 existing/added/skipped 三类清单

使用方式：
  from lark_field_helper import LarkFieldHelper

  helper = LarkFieldHelper()
  # 1) 拉取某字段当前所有选项
  opts = helper.list_options("Eje8bWtVdaPPPosu0GQcPclQnut", "tblnCWVGvCfFHW6m", "船公司")
  # 2) 对比并补齐
  result = helper.ensure_options(base_token, table_id, "船公司", ["SNL", "IAL", "新船公司X"])
  # result = {"existing": ["IAL"], "added": ["SNL", "新船公司X"], "skipped": []}

底层命令（通过 paramiko SSH 到 NAS 调用容器内 lark-cli）：
  lark base +field-list  --base-token X --table-id Y
  lark base +field-update --base-token X --table-id Y --field-id Z --json '{...}'
"""
import json
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

    # ---------- field-update ----------
    def add_options(self, base_token: str, table_id: str, field_id: str,
                    new_options: List[str], existing_options: List[str] = None) -> dict:
        """追加单选/多选字段的选项。
        lark-cli +field-update 用完整 PUT 语义：必须发送 name+type+options+multiple 等完整定义。
        """
        if not new_options:
            return {"added": [], "skipped": []}
        if existing_options is None:
            existing_options = []
        seen = set()
        merged = []
        for n in existing_options + new_options:
            if n and n not in seen:
                seen.add(n)
                merged.append({"name": n})
        if len(merged) == len(existing_options):
            return {"added": [], "skipped": new_options, "msg": "全部已存在"}

        # 先 find_field 拿完整字段定义
        cur = self.find_field(base_token, table_id, field_id)
        if not cur:
            return {"added": [], "skipped": new_options, "msg": "找不到字段 " + repr(field_id)}

        # 构造完整 PUT payload
        payload = {
            "name": cur.get("name", ""),
            "type": cur.get("type", "select"),
        }
        if cur.get("multiple") is not None:
            payload["multiple"] = cur["multiple"]
        # 单选/多选字段：options 放顶层（按 lark CLI 1.0.67 完整 PUT 语义）
        ft = payload["type"].lower()
        if ft in ("select", "singleselect", "multi-select", "multiselect"):
            payload["options"] = merged
        else:
            return {"added": [], "skipped": new_options, "msg": "字段类型 " + repr(payload["type"]) + " 不是 select"}

        json_str = json.dumps(payload, ensure_ascii=False)
        escaped = json_str.replace("\'", "\'\\\'\'")
        cmd = (
            "sudo docker exec " + self.config["container_name"] + " "
            "lark base +field-update --base-token " + base_token + " --table-id " + table_id + " "
            "--field-id " + field_id + " --json \'" + escaped + "\' --yes 2>&1"
        )
        out = self._exec(cmd, timeout=60)
        try:
            data = json.loads(out)
        except Exception:
            return {"added": [], "skipped": new_options, "msg": "field-update 返回非 JSON: " + out[:200]}
        if data.get("ok") is False:
            err = data.get("error", {})
            return {"added": [], "skipped": new_options, "msg": "field-update 失败: " + json.dumps(err, ensure_ascii=False)[:200]}
        return {"added": new_options, "skipped": [], "msg": "ok"}

    def ensure_options(self, base_token: str, table_id: str,
                       field_name: str, want_options: List[str]) -> dict:
        """确保给定值都在 select 字段选项里（缺则自动添加）。

        Returns dict:
          {
            "field_id": "fldXXXX",
            "field_name": "船公司",
            "field_type": "SingleSelect",
            "existing": [...],   # 已在选项中
            "added": [...],      # 本次新增
            "skipped": [...],    # 因 field 类型不支持等原因跳过
            "warnings": [...],
          }
        """
        result = {
            "field_id": "",
            "field_name": field_name,
            "field_type": "",
            "existing": [],
            "added": [],
            "skipped": [],
            "warnings": [],
        }
        if not want_options:
            return result
        fld = self.find_field(base_token, table_id, field_name)
        if not fld:
            result["warnings"].append(f"字段 {field_name!r} 不存在，跳过")
            result["skipped"] = list(want_options)
            return result
        result["field_id"] = fld.get("id") or fld.get("field_id", "")
        result["field_type"] = fld.get("type", "")
        if result["field_type"].lower() not in ("select", "multiselect", "singleselect"):
            result["warnings"].append(
                f"字段 {field_name!r} 类型为 {result['field_type']!r}，非单选/多选，跳过"
            )
            result["skipped"] = list(want_options)
            return result
        existing = self.list_options(base_token, table_id, field_name)
        to_add = [v for v in want_options if v and v not in existing]
        result["existing"] = [v for v in want_options if v in existing]
        if to_add:
            upd = self.add_options(base_token, table_id, result["field_id"],
                                   to_add, existing)
            if upd.get("added"):
                result["added"] = upd["added"]
            else:
                result["skipped"] = to_add
                result["warnings"].append(upd.get("msg", "添加失败"))
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