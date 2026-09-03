#!/bin/bash
# 1v1 chat 业务方重测后清理脚本 (可选)
# 用途: 业务方完成 1v1 chat 上传运价文件测试后, 如果想清理 FCL 表的测试数据, 跑此脚本
# 安全: 只删除最近 N 分钟内创建且含"PROBE"或测试关键字的记录
#
# 使用方法 (在 coco 容器内):
#   sudo docker exec -it Openclaw-coco bash
#   /home/node/.openclaw/workspace/skills/dg-rate-query/scripts/d68-post-test-cleanup.sh
#   # 或指定时间窗口:
#   /home/node/.openclaw/workspace/skills/dg-rate-query/scripts/d68-post-test-cleanup.sh 60
#
# 参数: $1 = 时间窗口分钟数 (默认 60 = 最近 1 小时内)

set -e

BASE_TOKEN="Eje8bWtVdaPPPosu0GQcPclQnut"
TABLE_ID="tblnCWVGvCfFHW6m"
WINDOW_MIN="${1:-60}"

echo "=== D68 post-test cleanup ==="
echo "FCL table: $TABLE_ID"
echo "Window: last $WINDOW_MIN minutes"
echo

# 1. 列出当前所有记录
echo "[1] Current FCL records:"
lark-cli --as user base +record-list \
  --base-token "$BASE_TOKEN" --table-id "$TABLE_ID" --format json 2>/dev/null \
  | python3 -c "
import sys, json
d = json.loads(sys.stdin.read())
items = d.get('data', {}).get('items', [])
fields = d.get('data', {}).get('fields', [])
print(f'  Total rows: {len(items)}')
for i, r in enumerate(items):
    fmap = dict(zip(fields, r))
    cn = fmap.get('合约号', '')
    print(f'  [{i}] contract_no={cn!r} record_id={r[0]}')
"
echo

# 2. 确认清理 (默认 yes = 仅删含 PROBE/TEST/D68 关键字的记录)
echo "[2] Cleanup records with PROBE/TEST keyword in 合约号 (last $WINDOW_MIN min):"
TO_DELETE=$(lark-cli --as user base +record-list \
  --base-token "$BASE_TOKEN" --table-id "$TABLE_ID" --format json 2>/dev/null \
  | python3 -c "
import sys, json
d = json.loads(sys.stdin.read())
items = d.get('data', {}).get('items', [])
fields = d.get('data', {}).get('fields', [])
for r in items:
    fmap = dict(zip(fields, r))
    cn = (fmap.get('合约号') or '').upper()
    if any(k in cn for k in ['PROBE', 'TEST', 'D68', 'D69', 'FINAL', 'E2E']):
        print(r[0])
")
echo "Will delete:"
echo "$TO_DELETE"
echo

# 3. 逐个删除
DELETED=0
for rid in $TO_DELETE; do
    if [ -n "$rid" ]; then
        echo "  Deleting $rid..."
        lark-cli --as user base +record-delete \
          --base-token "$BASE_TOKEN" --table-id "$TABLE_ID" \
          --record-id "$rid" --yes 2>/dev/null
        DELETED=$((DELETED + 1))
    fi
done
echo
echo "[3] Deleted $DELETED records"

# 4. 验证
echo "[4] Verification (should be 0 or only non-test records):"
ROWS=$(lark-cli --as user base +record-list \
  --base-token "$BASE_TOKEN" --table-id "$TABLE_ID" --format json 2>/dev/null \
  | python3 -c "import sys,json; print(len(json.loads(sys.stdin.read()).get('data',{}).get('items',[])))")
echo "  FCL rows: $ROWS"

# 5. 清 dedupe cache
echo "[5] Clear dedupe cache:"
rm -rf /tmp/dg-rate-query-write-cache/
echo "  Cleared /tmp/dg-rate-query-write-cache/"

echo
echo "=== Cleanup done. FCL + dedupe cache clean. ==="
