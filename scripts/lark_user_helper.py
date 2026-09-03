# -*- coding: utf-8 -*-
"""
飞书用户信息辅助工具
- 解析当前对话用户（通过 chat_id 反查 open_id）
- 名字 / 部门 / 邮箱 等信息查询
- 给 lark_rate_writer 传 [user][{id: open_id}] 格式

解决问题：
  - 导入人/审核人 字段需要飞书 open_id 格式 ou_xxx，不能传字符串
  - 当前对话用户是谁要能从 chat 上下文解析出来
  - 跨会话时也支持按 user_id/name 查询

底层调用：
  lark contact +user-get --user-id <open_id>
  lark im +chat-get --chat-id <chat_id>
  lark im +message-list --chat-id <chat_id> --limit 1
"""
import json
import os
from typing import Dict, Any, List, Optional

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


class LarkUserHelper:
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

    def _exec(self, cmd: str, timeout: int = 30) -> str:
        client = self._connect()
        try:
            _, stdout, stderr = client.exec_command(cmd, timeout=timeout)
            out = stdout.read().decode("utf-8", errors="replace").strip()
            err = stderr.read().decode("utf-8", errors="replace").strip()
            return out if out else err
        finally:
            client.close()

    def get_user(self, user_id: str) -> Dict[str, Any]:
        """查询用户信息（按 open_id）。

        Returns:
            {code, open_id, name, en_name, email, department, ...}
        """
        cmd = (
            f"sudo docker exec {self.config['container_name']} lark-cli "
            f"contact +user-get --user-id {user_id} 2>&1"
        )
        try:
            out = self._exec(cmd, timeout=15)
        except Exception as e:
            return {"code": "error", "msg": str(e)}
        try:
            data = json.loads(out)
        except Exception:
            return {"code": "error", "msg": "lark contact 返回非 JSON: " + out[:200]}
        if data.get("ok") is False:
            return {"code": "error", "msg": json.dumps(data, ensure_ascii=False)[:200]}
        u = data.get("data", {}).get("user", {}) or {}
        return {
            "code": "ok",
            "open_id": u.get("open_id", user_id),
            "name": u.get("name", ""),
            "en_name": u.get("en_name", ""),
            "email": u.get("email", ""),
            "department": ",".join(u.get("department_ids", []) or []),
            "raw": u,
        }

    def get_chat_latest_sender(self, chat_id: str) -> Dict[str, Any]:
        """获取某 chat 最新一条消息的发送者（即"当前对话用户"）。

        Returns:
            {code, open_id, name, message_id, chat_id, raw}
        """
        cmd = (
            f"sudo docker exec {self.config['container_name']} lark-cli "
            f"im +message-list --chat-id {chat_id} --limit 1 2>&1"
        )
        try:
            out = self._exec(cmd, timeout=15)
        except Exception as e:
            return {"code": "error", "msg": str(e)}
        try:
            data = json.loads(out)
        except Exception:
            return {"code": "error", "msg": "lark im +message-list 返回非 JSON: " + out[:200]}
        if data.get("ok") is False:
            return {"code": "error", "msg": json.dumps(data, ensure_ascii=False)[:200]}
        items = data.get("data", {}).get("items", [])
        if not items:
            return {"code": "error", "msg": "chat 无消息"}
        msg = items[0]
        sender = msg.get("sender", {}) or {}
        sender_id = sender.get("id", "") or sender.get("open_id", "")
        return {
            "code": "ok",
            "open_id": sender_id,
            "sender_id": sender_id,
            "chat_id": chat_id,
            "message_id": msg.get("message_id", ""),
            "raw": msg,
        }

    def resolve_user_ref(self, user_ref: str) -> List[Dict[str, Any]]:
        """把 user_ref 解析成 [{id: open_id}, ...] 格式，给 Bitable user 字段用。

        支持：
          - "ou_xxx" → [{id: "ou_xxx"}]
          - "user_name" / "姓名" → 先 whoami 拿当前 user，匹配名
          - "current" / "me" / "self" → 当前对话用户
          - "ai" / "agent" → []（不填）
          - "ou_xxx,ou_yyy" → [{id: "ou_xxx"}, {id: "ou_yyy"}]
        """
        if not user_ref:
            return []
        ref = user_ref.strip()
        if ref in ("", "ai", "agent", "AI-Agent", "AI", "bot"):
            return []
        if "," in ref or ";" in ref or "/" in ref:
            parts = [p.strip() for p in ref.replace(";", ",").split(",") if p.strip()]
            return [{"id": p} for p in parts]
        if ref.startswith("ou_") and len(ref) >= 8:
            return [{"id": ref}]
        # 名字解析：先 whoami
        who = self._exec(
            f"sudo docker exec {self.config['container_name']} lark-cli whoami 2>&1",
            timeout=10
        )
        try:
            who_data = json.loads(who)
            if who_data.get("ok") is not False:
                me = who_data.get("data", {}) or {}
                if ref in (me.get("name"), me.get("en_name"), "me", "self", "current"):
                    oid = me.get("open_id") or me.get("user_id") or ""
                    if oid:
                        return [{"id": oid}]
        except Exception:
            pass
        # fallback: 把名字当 open_id 假设
        return [{"id": ref}]


def _main():
    import argparse
    ap = argparse.ArgumentParser(description="飞书用户信息辅助")
    ap.add_argument("--action", choices=["get", "chat-sender", "resolve"], required=True)
    ap.add_argument("--user-id", help="open_id (action=get)")
    ap.add_argument("--chat-id", help="chat_id (action=chat-sender)")
    ap.add_argument("--ref", help="user_ref (action=resolve)")
    args = ap.parse_args()
    h = LarkUserHelper()
    if args.action == "get":
        print(json.dumps(h.get_user(args.user_id), ensure_ascii=False, indent=2))
    elif args.action == "chat-sender":
        print(json.dumps(h.get_chat_latest_sender(args.chat_id), ensure_ascii=False, indent=2))
    elif args.action == "resolve":
        print(json.dumps(h.resolve_user_ref(args.ref), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _main()