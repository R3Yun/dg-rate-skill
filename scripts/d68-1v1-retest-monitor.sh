#!/bin/bash
# D68 1v1 retest 后台监控 (Agent-side)
# 业务方执行 1v1 chat 上传后, 自动捕获 FCL 表状态变化
# 用法: 启动后台运行, 定期 (60s) 检查 FCL 行数
#       当行数 > 0 且合约号含 PROBE/TEST/D68/D69/FINAL/E2E 标记 = 业务方重测开始
#       或 业务方手动触发: touch /tmp/d68-retest-triggered (立即报告当前状态)

set -e

BASE_TOKEN="Eje8bWtVdaPPPosu0GQcPclQnut"
TABLE_ID="tblnCWVGvCfFHW6m"
LOG=/home/node/.openclaw/workspace/.test_monitor/2026-08-10-d68-1v1-retest-monitor.log
STATE=/tmp/d68-retest-monitor.state
mkdir -p "$(dirname $LOG)"

# 测试关键字 (匹配合约号)
KEYWORDS='PROBE|TEST|D68|D69|FINAL|E2E|IAL|CNSHA'

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; }

get_fcl_count() {
  lark-cli --as user base +record-list \
    --base-token "$BASE_TOKEN" --table-id "$TABLE_ID" --format json 2>/dev/null \
    | python3 -c "import sys,json; print(len(json.loads(sys.stdin.read()).get('data',{}).get('items',[])))" 2>/dev/null || echo 0
}

get_test_records() {
  lark-cli --as user base +record-list \
    --base-token "$BASE_TOKEN" --table-id "$TABLE_ID" --format json 2>/dev/null \
    | python3 -c "
import sys, json
d = json.loads(sys.stdin.read())
items = d.get('data', {}).get('items', [])
fields = d.get('data', {}).get('fields', [])
for r in items:
    fmap = dict(zip(fields, r))
    cn = (fmap.get('合约号') or '')
    if any(k in cn for k in ['PROBE','TEST','D68','D69','FINAL','E2E']):
        print(r[0], cn)
" 2>/dev/null
}

snapshot() {
  echo "--- snapshot $(date '+%H:%M:%S') ---"
  echo "FCL rows: $(get_fcl_count)"
  echo "Test records:"
  get_test_records
  echo
}

# 启动
log "D68 1v1 retest monitor started (PID=$$)"
log "Watching FCL table for test data..."
snapshot | tee -a "$LOG"
PREV_COUNT=$(get_fcl_count)
echo "$PREV_COUNT" > "$STATE"

while true; do
  CUR=$(get_fcl_count)
  TRIGGER="/tmp/d68-retest-triggered"
  if [ "$CUR" != "$(cat $STATE 2>/dev/null || echo 0)" ] || [ -f "$TRIGGER" ]; then
    log "FCL changed: $(cat $STATE) -> $CUR"
    snapshot | tee -a "$LOG"
    if [ -f "$TRIGGER" ]; then rm -f "$TRIGGER"; fi
  fi
  echo "$CUR" > "$STATE"
  sleep 60
done
