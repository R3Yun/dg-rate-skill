#!/usr/bin/env python3
"""根据运价编号查找 record_id"""
import json
import subprocess
import sys

def find_record_by_rate_no(rate_no: str, base_token: str = "Eje8bWtVdaPPPosu0GQcPclQnut", table_id: str = "tblnCWVGvCfFHW6m"):
    """通过运价编号查找飞书记录 ID"""
    # 格式化运价编号 (确保是 NO.xxx 格式)
    if not rate_no.startswith("NO."):
        rate_no = f"NO.{rate_no}"
    
    # 使用 lark-cli 搜索记录
    cmd = [
        "lark-cli", "--as", "user", "base", "+record-search",
        "--base-token", base_token,
        "--table-id", table_id,
        "--keyword", rate_no,
        "--search-field", "运价编号",
        "--format", "json"
    ]
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return None, f"搜索失败: {result.stderr}"
        
        data = json.loads(result.stdout)
        if not data.get("ok"):
            return None, f"API 错误: {data.get('error', {})}"
        
        # 从 record_id_list 获取记录 ID
        record_id_list = data.get("data", {}).get("record_id_list", [])
        if not record_id_list:
            return None, f"未找到运价编号为 {rate_no} 的记录"
        
        # 返回第一个匹配的记录 ID
        record_id = record_id_list[0]
        return record_id, None
        
    except subprocess.TimeoutExpired:
        return None, "搜索超时"
    except Exception as e:
        return None, f"搜索异常: {str(e)}"

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python find_record_by_rate_no.py <运价编号>")
        sys.exit(1)
    
    rate_no = sys.argv[1]
    record_id, error = find_record_by_rate_no(rate_no)
    
    if error:
        print(json.dumps({"ok": False, "error": error}, ensure_ascii=False))
        sys.exit(1)
    else:
        print(json.dumps({"ok": True, "record_id": record_id, "rate_no": rate_no}, ensure_ascii=False))
