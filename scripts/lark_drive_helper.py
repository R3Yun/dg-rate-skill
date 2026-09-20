# -*- coding: utf-8 -*-
"""
飞书云盘（Drive）辅助工具
把本地源文件（Excel/图片/PDF/聊天记录 txt）上传到飞书云盘指定目录，返回 share URL。

解决问题：
  - 运价库多维表格中「源文件链接」字段需要可点击的 URL
  - 用户上传的运价文件原始数据可追溯，点击链接直接打开预览
  - 避免每次写记录都要用户手动传文件到云盘

使用：
  from lark_drive_helper import LarkDriveHelper
  helper = LarkDriveHelper()
  result = helper.upload("/tmp/兴亚船公司运价表.xls", folder_name="DG-Logistics/origin-files")
  # result = {"file_token": "...", "share_url": "https://...", "name": "..."}

底层调用 lark drive +upload 等子命令。
"""
import json
import os
import shlex
import time
from typing import Dict, Any, Optional

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
    # v3.10.5: lark-cli 1.0.76 改 --folder 为 --folder-token (需要 token 而非路径)
    # 留空表示上传到根目录; 如需固定目录, 在外部调用时传 folder_token
    "default_folder_token": "",
}


def _parse_cli_json(output: str) -> Dict[str, Any]:
    """解析 lark-cli JSON；兼容上传进度行位于 JSON 之前。"""
    text = str(output or "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        if start < 0:
            raise
        data, _ = json.JSONDecoder().raw_decode(text[start:])
        return data


class LarkDriveHelper:
    def __init__(self, config: Dict[str, Any] = None):
        self.config = {**DEFAULT_CONFIG, **(config or {})}
        if not HAS_PARAMIKO:
            raise ImportError("paramiko 未安装")

    def _in_container(self) -> bool:
        """检测是否运行在 OpenClaw-coco 容器内 (同 lark_rate_writer).

        容器内直连 lark-cli (免 SSH/docker exec 中转); 外部环境走 SSH fallback.
        判据: coco 容器独有 lark 二进制存在 (目录可写判据在 OpenCode 环境也成立, 不可靠).
        """
        lark_bin = os.environ.get("LARK_BIN", "/home/node/.openclaw/workspace/bin/lark")
        return os.path.exists(lark_bin)

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

    def _exec(self, cmd: str, timeout: int = 120) -> str:
        client = self._connect()
        try:
            _, stdout, stderr = client.exec_command(cmd, timeout=timeout)
            out = stdout.read().decode("utf-8", errors="replace").strip()
            err = stderr.read().decode("utf-8", errors="replace").strip()
            return out if out else err
        finally:
            client.close()

    def upload(self, local_path: str, folder_name: str = None,
               file_name: str = None) -> Dict[str, Any]:
        """上传本地文件到飞书云盘指定目录，返回 file_token + share_url。

        Args:
            local_path: 容器内可访问的本地文件路径
            folder_name: 目标文件夹 token（默认空 = 上传到调用者云盘根目录）
            file_name: 覆盖默认文件名（默认用 basename(local_path)）

        Returns:
            dict: {code, file_token, share_url, name, folder, raw}
        """
        if not os.path.isfile(local_path):
            return {"code": "error", "msg": "文件不存在: " + local_path}
        folder = folder_name or self.config.get("default_folder_token") or ""
        name = file_name or os.path.basename(local_path)
        # D81 (2026-08-29): 容器内直连 lark-cli (免 SSH/docker exec 中转, 消除 SSH 端口/网络故障点)
        if self._in_container():
            return self._upload_local(local_path, name, folder)
        # 外部环境: SSH → docker exec → lark-cli (fallback)
        # lark-cli 1.0.76 拒绝绝对路径和包含 ``..`` 的路径。
        # 让 docker exec 的工作目录切到源文件目录，只传 basename。
        container_workdir = os.path.dirname(os.path.abspath(local_path)) or "/"
        container_local = os.path.basename(local_path)
        # v3.10.5: lark-cli 1.0.76 把 --folder 改名为 --folder-token (需要 token 而非路径)
        # 没有 token 时省略 --folder-token, 上传到根目录
        # 调用 lark drive +upload --file <path> [--folder-token <token>] --name <name>
        cmd_parts = [
            "sudo", "docker", "exec", "-w", container_workdir,
            self.config["container_name"],
            "/home/node/.openclaw/workspace/bin/lark", "--as", "user",
            "drive", "+upload", "--file", container_local,
        ]
        if folder:
            cmd_parts.extend(["--folder-token", folder])
        cmd_parts.extend(["--name", name])
        cmd = " ".join(shlex.quote(part) for part in cmd_parts) + " 2>&1"
        try:
            out = self._exec(cmd, timeout=300)
        except Exception as e:
            return {"code": "error", "msg": "上传失败: " + str(e), "raw": ""}
        return self._parse_upload_output(out, name, folder)

    def _upload_local(self, local_path: str, name: str, folder: str) -> Dict[str, Any]:
        """容器内直连 lark-cli drive +upload (cwd=源目录, 传 basename)."""
        import subprocess
        cmd = [
            os.environ.get("LARK_BIN", "/home/node/.openclaw/workspace/bin/lark"),
            "--as", "user", "drive", "+upload",
            "--file", os.path.basename(local_path),
        ]
        if folder:
            cmd.extend(["--folder-token", folder])
        cmd.extend(["--name", name])
        try:
            r = subprocess.run(
                cmd,
                cwd=os.path.dirname(os.path.abspath(local_path)) or "/",
                capture_output=True, text=True, timeout=300,
            )
        except Exception as e:
            return {"code": "error", "msg": "上传失败: " + str(e), "raw": ""}
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        return self._parse_upload_output(out, name, folder)

    def _parse_upload_output(self, out: str, name: str, folder: str) -> Dict[str, Any]:
        """解析 lark drive +upload 输出 → {code, file_token, share_url, ...}."""
        # 解析 lark-cli 返回
        try:
            data = _parse_cli_json(out)
        except Exception:
            return {"code": "error", "msg": "lark drive 返回非 JSON: " + out[:300], "raw": out}
        if data.get("ok") is False:
            return {"code": "error", "msg": "上传失败: " + json.dumps(data, ensure_ascii=False)[:300], "raw": out}
        # 提取 token + url
        result = data.get("data", {}) or {}
        file_token = result.get("file_token") or result.get("token") or ""
        # lark-cli 通常返回 url 或需要额外请求
        share_url = result.get("url") or result.get("share_url") or ""
        if not share_url and file_token:
            # 拼一个标准飞书预览链接
            share_url = "https://feishu.cn/file/" + file_token
        return {
            "code": "ok",
            "file_token": file_token,
            "share_url": share_url,
            "name": name,
            "folder": folder,
            "raw": out[:500],
        }

    def batch_upload(self, file_paths: list, folder_name: str = None) -> list:
        """批量上传多个文件。"""
        results = []
        for fp in file_paths:
            r = self.upload(fp, folder_name)
            results.append({"source_path": fp, **r})
        return results


def _main():
    import argparse
    ap = argparse.ArgumentParser(description="飞书云盘上传")
    ap.add_argument("--file", required=True, help="容器内文件绝对路径")
    ap.add_argument("--folder", default=None, help="目标文件夹（默认 DG-Logistics/origin-files）")
    ap.add_argument("--name", default=None, help="覆盖文件名")
    args = ap.parse_args()
    h = LarkDriveHelper()
    r = h.upload(args.file, args.folder, args.name)
    print(json.dumps(r, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _main()
