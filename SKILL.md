# 运价查询 Skill (dg-rate-query)

> v3.10（§关键字段全字段详解, 2026-07-21）。
> 详细设计见 [docs/04 §附录 D](../../docs/04-rate-management.md)；测试见 [docs/40 §17.7](../../docs/40-test-plan.md)；字段顺序清单见 [docs/fcl-field-reorder-checklist-20260721.md](../../docs/fcl-field-reorder-checklist-20260721.md)。

## 何时调用

用户聊到运价、报价单、海运费表、Cargoware 导入、运价库查询时启用本 skill。

## P0 执行闸门

### 写库前
1. LLM 先检查 6 个 P0（POL/POD/P/C/有效期起/有效期止/订舱代理）；任一缺失（特别是 P/C）→ 列出全部缺失并问业务人员，**不调用写工具**
2. wrapper 再做原子闸门；任一记录缺 P0 → 返回 `CRITICAL_FIELDS_MISSING`、`written=0`，整批不调用飞书 API
3. 每条记录必须含非空 `_provenance.source_file` + `_provenance.parser`；缺失或空值 → `MISSING_PROVENANCE`
4. 只在 `code=ok`、`success=true`、`written=N` 且 N 个 `record_ids` 齐全时回复成功
5. 缺字段只用普通文字一次性询问；不得主动发送 interactive 选择/确认卡片，业务人员用文字补充和确认

### 运价查询预览（业务预览唯一事实源）
1. **唯一事实源** = `dg-rate-query export-cw --from-feishu [筛选参数] --dry-run` 的最终输出
   - 筛选参数（可组合使用）：
     - `--row-range <起>-<止>`: 按运价编号范围筛选
     - `--rate-no <编号>`: 按运价编号筛选（支持逗号分隔多个）
     - `--carrier <船公司>`: 按船公司模糊匹配
     - `--pol <港口>`: 按起运港模糊匹配
     - `--pod <港口>`: 按目的港模糊匹配
     - `--import-after <日期>`: 按导入时间起始筛选
     - `--import-before <日期>`: 按导入时间截止筛选
2. 禁止改写 / 重排 / 重解释 `[SOURCE=FEISHU]` / `[SCHEMA_AUTHORITATIVE]` / `[EMPTY_SOURCE_FIELDS]` 标记
3. `20GP`/`40GP`/`40HQ` 标签**不可互换**；不在 dry-run 输出里的值不许猜

### 文件附件
- **群聊**：附件必须与 `@可可` 在同一条 post 消息内
- **1v1**：用真实 `MediaPath` 调 `dg_rate_query_ocr_image`；失败如实报告，不写库
- 不许编历史附件路径


### 🚨 STOP/暂停 硬规则 (P0, 2026-07-24 1v1 E2E 触发)

业务人员发"停止/暂停/不要写/先停/立刻停"等指令时:

- ❌ 禁止：继续推进已启动的 batch_write 序列
- ❌ 禁止：在业务人员没有再次发"继续"前, 启动新的 batch_write / write_record
- ✅ 必须：立即收尾当前正在飞的那一批 batch_write, 然后 task_update --status 待确认
- ✅ 必须：每次 batch_write 后, 在 reply 中说明"已写 X 条, 还剩 Y 条" + "是否继续" 提示, 让业务人员有节点可停

**触发案例**: 2026-07-24 多 Sheet E2E 中, 业务 10:10 说"暂停"可可继续写 31 条; 10:20 说"立刻停止"可可继续写 128 条后才停。

**验证方法**: task.json 中 `execution.last_action` 应在 "停止" 指令后立刻为 `awaiting_user_confirmation`, 不应继续 `batch_written`。

### 代码层强制 (v3.10.6.1, D16)
- `dg_rate_query_batch_write` / `dg_rate_query_write_record` 入口检查 `is_user_stopped(task)` → 若 true 返回 `code: USER_STOPPED`, 不调 batch-write
- `task_state.is_user_stopped(task)` 判定: `pending_action == "user_stopped"` 或 `execution.last_action == "awaiting_user_confirmation"`
- `batch-write.py` CLI 加可选 `--chat-id` 参数, 传了则同步执行 STOP 守卫 (raw CLI 兜底)
- LLM 收到 USER_STOPPED: 必须停手, 调 `dg_rate_query_task_find` 检视, 询问业务确认后再继续
- 详见: `docs/decisions/20260729-v31061-p0a-stop-guard.md`

## 调用范式

所有子命令走统一 wrapper：

```bash
dg-rate-query <subcmd> [flags]
```

公共 flag：
- (v3.7+: --import-user/--review-user 已废弃, 保留为 no-op)
- 导出筛选参数（可组合使用）：
  - `--row-range <起>-<止>`: 按运价编号范围筛选
  - `--rate-no <编号>`: 按运价编号筛选（支持逗号分隔多个）
  - `--carrier <船公司>`: 按船公司模糊匹配
  - `--pol <港口>`: 按起运港模糊匹配
  - `--pod <港口>`: 按目的港模糊匹配
  - `--import-after <日期>`: 按导入时间起始筛选
  - `--import-before <日期>`: 按导入时间截止筛选
- **业务规则 (v3.10.10)**: 不限有效期/状态, 仅按用户筛选条件过滤
- 默认输出文件名: `cargoware_export_YYYYMMDD_HHMMSS.xls` (本地时间, v3.10.10)
- 空结果不得生成或上传空模板
- `--dry-run` 预演

## 核心原子工具（D6 拆薄）

| 子命令 | 作用 | 替换 |
|---|---|---|
| `read-xlsx` | xlsx → JSON+markdown | 原 `inspect_excel`+`excel_helper` |
| `read-xls` | xls 同上 | `parse_file` 重型 |
| `read-csv` | csv 同上 | `parse_file` |
| `read-txt` | 自动编码探测 + 中断行 | `parse_file` |
| `ocr-image` | OCR（走 table-ocr / MinerU） | `parse-image` 重型 |
| `port-resolve <UNLOCODE>` | 单港 → 区域/国家 | `port_resolver` 主体 |
| `port-batch-resolve <json>` | 批量港口解析 | — |
| `write-record <json>` | 单条写，强制 `_provenance`；支持 `--record-id/--rate-no/--merge` 更新 | `write-lark` 重壳 |
| `write-record` (筛选模式) | 按 `--rate-no/--carrier/--pod/--pol/--import-after/--import-before` 批量更新 | — |
| `find-records` (筛选查找) | 按筛选条件返回每条 record_id + 关键字段 (D93: 含 `--booking-agent` 订舱代理归一化精确匹配 + `--valid-on/--valid-after/--valid-before` 有效期窗口) | — |
| `delete-record` | 按筛选条件删除记录（默认 `--dry-run`；实际删需 `--yes`；lark-cli 二次确认已自动） | — |
| `batch-write <json>` | 批量写，强制每条 `_provenance` | 旧 `write-batch` 重壳 |
| `export-cw` | 导出 Cargoware xls | 保留 |

**已删除**：`parse`、`adapt` (LLM-Adaptive)、`write-lark` 重壳、`write-batch`、`quality-backfill`（反 D7）、`parse-image` 重型链。

### 🚨 D46 硬规则：`_active_task_warning` 必处理 (2026-08-07)

**触发场景**: 调用 `dg_rate_query_ocr_image` 后, 若返回 JSON 顶层出现 `active_task_blocked: true` 或 `_active_task_warning` 字段 (任一), 说明**该 chat_id 已有进行中的 task**, 新 OCR task 被 `RateTaskStore` 拒绝创建.

**必须行为 (P0, 违反 = 业务方体验崩坏)**:

1. **禁止**继续调用 `dg_rate_query_build_draft` / `dg_rate_query_batch_write` (会创建新 task, 加重阻塞或踩 P0 闸门)
2. **必须先告知业务方**, 消息模板:
   ```
   ⚠️ 检测到进行中的 task: {active_task_id}
   来源: {active_task_msg} (通常含 source_file)
   
   请选择:
   (1) 关闭旧 task → 我帮你跑 `python3 task_state.py close {active_task_id} --reason abandoned` 后重新 OCR
   (2) 继续当前 OCR → 用 write_record 绕过 build_draft (OCR 路径无 parse_id)
   ```
3. **必须等待业务方明确选择**, 不可自行决定
4. **如业务方选 (1)**: 关闭后, 重新调 `dg_rate_query_ocr_image`, 验证 `active_task_blocked: false` 后再继续
5. **如业务方选 (2)**: 直接调 `dg_rate_query_write_record` (单条) 写入, 不要调 batch_write / build_draft

**为什么不能静默吞掉** (历史教训 D44, 2026-08-06 1v1 测试发现):
- v3.11 之前 `_create_parse_workspace` 静默吞 `ActiveTaskExists` 异常, 业务方**完全不知道**有 active task 阻塞
- LLM 看到 `entry_count=0` + `task_id=""` 误以为 OCR 失败, 重试浪费 token
- D44 修复 Python 层返回 `active_task_blocked=True`, D46 修复 Plugin 层把它**提升到顶层** + 加人类可读 warning

**验证方法**:
- 看 OCR 返回 JSON 顶层是否有 `active_task_blocked` 字段
- 看到 `true` → 走上面 5 步流程
- 看到 `false` 或字段不存在 → 正常继续

## 接口契约

```jsonc
// skill → LLM (读回)
{
  "source_summary": {"file": "IAL.xlsx", "size_kb": 120, "sheet_count": 2},
  "content_markdown": "| POL | POD | Carrier | 20GP |\n...",
  "reading_hint": "识别为多 sheet 多 carrier, 按 sheet 拆分"
}

// LLM → skill (写入)
{
  "records": [
    {
      "fields": {"POL":"CNSHA","POD":"THBKK","Carrier":"MSK","20GP":1500},
      "_provenance": {"source_file":"IAL.xlsx","sheet":"Sheet1","row":3,"parser":"read-xlsx"}
    }
  ]
}
```

缺 `_provenance` → 拒写（exit≠0，错误码 `MISSING_PROVENANCE`）。

## 字段顺序 (2026-07-31 Q3 Step10 截图同步)

代码侧 `_write_batch.fields` 列表与 OPTIONAL_FIELDS 列表, 已对齐飞书 FCL 表 UI 实际顺序 (业务人员手动调整后的顺序, 不是"逻辑顺序", 业务人员安排为准)。

**Q3 Step10 更新 (2026-07-31)**: 根据用户飞书截图, 字段顺序调整:
- 5→6: 新增 "起运港全称" "目的港全称" (port_resolver 自动填入中文全称)
- 12→13: 班期 移到 船名 之后
- 13→14: 新增 "船名" (已存在, 移位置)
- 14→15: 新增 "航次"
- 15→16: 新增 "ETD" "ETA"
- 16→18: 航程 移到 4 个新字段之后
- 18: 订舱代理 移到 航程 之后
- 25→27: 新增 "40GP DG(USD)" "40HQ DG(USD)" "20GP DG(USD)" (v3.8.7 决策 C, 之前 schema 未列)
- 30: 免柜期 移到 AMS 之后
- 36: 备注 移到 超重备注 之后
- 39: 导入时间 移到 解析置信度 之后

代码侧 `_write_batch.fields` 列表与本节描述完全一致。

飞书 lark-cli 不支持字段位置调整 (`+field-move` 不存在), 字段顺序只能在飞书 UI 手动拖拽。本节不需要业务人员再做任何操作。

## 关键字段 (Key Fields Reference, v3.9+)

> **LLM 必读**: 所有解析/写入操作前必须通读本章节。包含字段全景速览 + 字段详解 + 备注提取规则 + 解析错误 Top 10 + 中英术语表。

### 8.1 字段全景速览

按必填级别分组 (来源: `skills/dg-rate-query/scripts/rate_io.py`):

| 级别 | 数量 | 字段 | 行为 |
|---|---|---|---|
| **CRITICAL (P0)** | 6 | POL / POD / P/C / 有效期起 / 有效期止 / 订舱代理 (D80) | 缺失即拒收 (exit≠0) |
| **P1 必问** | 2 | 船公司 / 至少 1 个价格 | 缺失写入「待补充」状态 |
| **P2 提示** | 9 | 币种 / 起运区域 / 目的区域 / 班期 / 船名 / 航次 / ETD / ETA / 免柜期 | 缺失正常入库 + 提示业务人员 |
| **OPTIONAL** | 12+ | 合约号 / 20NOR / 40NOR / 45尺 / AMS / ENS / 超重备注 / DG 附加费 等 | 列存在, 单元格可空 |
| **AUTO** | 3 | 状态 / 解析置信度 / 导入时间 | Agent 自动填, 不视为缺失 |
| **元数据** | 2 | 原文件附件 / 运价编号 | 自动/手工, 不影响校验 |

### 8.2 CRITICAL 字段详解 (6 个, D80: +订舱代理)

#### POL — Port of Loading 起运港 / 装货港
- **代码 attr**: `pol`
- **飞书 FCL ID**: `fldKtetOx2` (text)
- **必填级别**: P0 (拒收)
- **含义**: 货物装船启运的港口
- **格式**: 英文 UN/LOCODE 5 位大写字母 (如 `CNSHA` / `SGSIN` / `THBKK` / `USLAX`)
- **来源示例**: `"POL: Shanghai"`, `"POL=CNSHA"`, `"起运港 上海"`
- **解析规则**: 用 `port_resolver` 把中文别名 (上海/曼谷/林查班) 转 UN/LOCODE
- **同义词**: 装货港 / 起运港 / Load Port / Origin Port
- **常见错误**:
  - ❌ `CNSHA上海` (中英混排)
  - ❌ `CNSHA, China` (带国家名)
  - ❌ `Shanghai` (中文拼音, 非 UN/LOCODE)
  - ❌ 空字符串 / null
- **D7 硬约束**: 不能用历史默认值 (如看到历史都是 CNSHA, 也不能默认填), 必须问业务人员

#### POD — Port of Destination 目的港 / 卸货港
- **代码 attr**: `pod`
- **飞书 FCL ID**: `fldO48DLjK` (text)
- **必填级别**: P0 (拒收)
- **含义**: 货物最终卸船的目的港口
- **格式**: 英文 UN/LOCODE 5 位大写字母 (如 `THBKK` / `USNYC` / `DEHAM`)
- **来源示例**: `"POD: Bangkok"`, `"POD=THBKK"`, `"目的港 曼谷"`
- **解析规则**: 同 POL, 用 `port_resolver` 转 UN/LOCODE
- **同义词**: 卸货港 / 目的港 / Discharge Port / Destination Port
- **常见错误**: 同 POL (中英混排 / 带国家名 / 中文)
- **D7 硬约束**: 同 POL, 不能用历史默认值

#### P/C — Port/CY 服务范围 / 交接范围
- **代码 attr**: `pc`
- **飞书 FCL ID**: `fldbTLNcdA` (text)
- **必填级别**: P0 (拒收)
- **含义**: CargoWare 运价模板中的 Port/CY 服务范围；必须以源文件或业务人员确认值为准
- **格式**: 文本, 当前业务常见值只使用 `Both` / `CY` / `Port`
- **来源示例**: `"P/C: Both"`, `"P/C=CY"`, `"P/C Port"`
- **解析规则**:
  - `Both` / `均可` → `Both`
  - `CY` / `堆场` → `CY`
  - `Port` / `港口` → `Port`
- **同义词**: Port/CY / 服务范围 / 交接范围
- **常见错误**:
  - ❌ 空字符串 / 漏问 (但此为 CRITICAL, 必须列在缺失清单)
  - ❌ 把 FILO/FIO 当成 P/C；它们是运价条款，不是本字段选项
  - ❌ 把 P/C 解释成 Prepaid/Collect；本项目该字段按 CargoWare 模板的 `Both/CY/Port` 使用
- **D7 硬约束**: 必须列出（CRITICAL，不能漏问）；没有 P/C 就问业务人员，**绝不默认 `Both`**
- **与常见易混字段区分** (v3.10.5, 防幻觉):
  - ❌ **FILO / FIO** 不是 P/C, 不得用二选一方式追问
  - ❌ **THC** (Terminal Handling Charge) 不是 P/C, 是附加费/费项, 不要填到 P/C 字段
  - ❌ **FAK** (Freight All Kinds) 不是 P/C, 是运价类型/费率术语, 不要填到 P/C 字段
  - ❌ **DG** (Dangerous Goods surcharge) 不是 P/C, 是危险品附加费, 应写入 `20GP DG(USD)`/`40GP DG(USD)`/`40HQ DG(USD)` 字段
  - ✅ P/C 当前只使用 `Both` / `CY` / `Port` 之一；源文件出现其他值时原样预览并让业务人员确认

#### 有效期起 — Valid From / Effective Date From
- **代码 attr**: `valid_from`
- **飞书 FCL ID**: `fldCZtkrvy` (datetime)
- **必填级别**: P0 (拒收)
- **含义**: 运价生效起始日期
- **格式**: `yyyy-MM-dd` 或完整 ISO 8601 `yyyy-MM-dd HH:mm:ss` (飞书 datetime, 实际接受完整 ISO)
- **来源示例**: `"Valid: 2026-07-01 ~ 2026-07-31"`, `"有效期 2026/07/01"`
- **解析规则**:
  - `2026-07-01` / `2026/07/01` / `07-01-2026` / `1 Jul 2026` → `2026-07-01`
  - "From: 2026-07-01" / "自 2026-07-01 起" → 提取日期
  - 无明确起期但有"截止日期 X" → 起期可能 = 报价单签发日 (问业务人员)
- **同义词**: 有效期起 / 生效日期 / Valid From / Effective From / Start Date / 起始日期
- **常见错误**:
  - ❌ `2026-7-1` (缺零填充, 飞书 datetime 不接受)
  - ❌ `2026/7/1` (单数字月份/日期, 应转 `2026-07-01`)
  - ❌ `7月1日` (中文日期格式)
  - ❌ 起期晚于止期 (数据错误)

#### 有效期止 — Valid To / Effective Date To
- **代码 attr**: `valid_to`
- **飞书 FCL ID**: `fld0O5Wobz` (datetime)
- **必填级别**: P0 (拒收)
- **含义**: 运价失效截止日期
- **格式**: `yyyy-MM-dd` 或完整 ISO 8601 `yyyy-MM-dd HH:mm:ss` (飞书 datetime, 实际接受完整 ISO)
- **来源示例**: 同有效期起, "Valid to: 2026-07-31" / "有效期至 2026/07/31"
- **解析规则**: 同有效期起
- **同义词**: 有效期止 / 截止日期 / Valid To / Effective To / End Date / Expiry Date
- **常见错误**:
  - 同有效期起
  - ❌ 起期晚于止期 (校验)
  - ❌ 止期早于当前日期 (已过期, 但仍可入库, 仅 quality_scan 提示)

### 8.3 关键 OPTIONAL 字段详解

#### 船公司 — Carrier / Shipping Line
- **代码 attr**: `carrier`
- **飞书 FCL ID**: `fldh2VRr8A` (text, v3.7+ 改文本字段)
- **必填级别**: P1 (必问, 缺失写「待补充」)
- **含义**: 提供海运服务的船公司代码 (2-4 位 SCAC 代码或简称)
- **格式**: 文本, 常见值: `MSK` (马士基) / `MSC` / `CMA` / `COSCO` / `OOCL` / `ONE` / `HMM` / `YML` / `SITC` / `ZIM` / `KMTC` / `IAL` 等
- **代码示例**: `EMC` = 长荣海运 (Evergreen)；`OOCL` = 东方海外；没有可靠来源时只复述代码，不擅自展开中文名
- **来源示例**: `"Carrier: MSK"`, `"船公司 马士基"`, `"Line: COSCO"`
- **同义词**: 船公司 / 航商 / Carrier / Shipping Line / Line / Operator
- **v3.7+ 简化**: 不再做 select option 校验, 新船公司直接写文本; 港口代码表查不到的也原样写表 (不污染字典)

#### 订舱代理 — Booking Agent
- **代码 attr**: `booking_agent`
- **飞书 FCL ID**: `fld98P0EV5` (text, v3.7+ 改文本字段)
- **必填级别**: **P0** (D80 升级; 缺失即拒收)
- **含义**: 实际接受订舱的货代公司 (可能是船公司直代或 NVOCC)
- **格式**: **订舱口中文名称** (如 兴亚船务有限公司) — D80: `build_draft` 按 `carrier` 自动匹配订舱口主数据写入; 导出时反查 CargoWare 代码
- **来源示例**: `"Booking Agent: KEYUN"` / `"订舱代理 克运国际物流"` / `"代理: 上海宏盛"`
- **同义词**: 订舱代理 / 货代 / Booking Agent / Agent / NVOCC
- **D80 规则**: 匹配不到 (carrier 不在订舱口) 或命中多客商共用代码 (SITC/HAMBURG SUD) → 问业务人员

#### 20GP/40GP/40HQ O/F — Ocean Freight 海运费 (基础价格)
- **代码 attr**: `of_20` / `of_40` / `of_40hq`
- **飞书 FCL ID**:
  - `20GP O/F(USD)` → `fldtxsWEd2`
  - `40GP O/F(USD)` → `fld4NZ5Hjf`
  - `40HQ O/F(USD)` → `fldt34a32J`
- **类型**: number currency USD (精度 2 位)
- **必填级别**: P1 (必问, 三者至少一个)
- **含义**: 单个集装箱的海运基础运费 (不含附加费)
- **格式**: 数字, 如 `1500.00` (USD)
- **来源示例**: `"20' GP: USD 800"`, `"40HQ: $1,500"`, `"20GP=800;40GP=1500"`
- **同义词**: 海运费 / 基础运费 / O/F / Ocean Freight / Base Freight
- **D7 硬约束**:
  - 至少 1 个价格 (20GP/40GP/40HQ 任一)
  - 不可互换标签: `20GP`/`40GP`/`40HQ` 不可互换
  - 仅复述 dry-run 终态, 不在输出里的值不许猜

#### 20GP/40GP/40HQ DG — Danger Goods Surcharge 危险品附加费 (v3.8.7 增强 2026-07-21)
- **代码 attr**: `dg_20` / `dg_40` / `dg_40hq`
- **飞书 FCL ID**:
  - `20GP DG(USD)` → `fldpOkjHyY`
  - `40GP DG(USD)` → `fldUYMl5Tk`
  - `40HQ DG(USD)` → `fldM5sfUJa`
- **类型**: number currency USD (精度 2 位)
- **含义**: 危险品 (Class 1-9) 集装箱的海运附加费
- **格式**: 数字, 如 `150.00` / `300.00` / `350.00` (USD)
- **必填级别**: **P1 必填（v3.8.7 决策 C）** — 业务数据有 DG 时 Agent 必须拆 150/300/350 到 3 个数字字段
- **写入路径**:
  - **3 个数字字段同时填**（解析时直接拆，不用等运营补救）
  - **备注字段冗余**（格式 `DG USD 150/300` 或 `DG USD 150/300/350`，业务人员可读）
  - 非危险品运价（无 DG 加价）3 个数字字段留空，备注也不写 DG
- **来源示例**: `"DG: USD 150/300/350"`, `"危险品附加费 20GP=150, 40GP=300"`, `"DG USD 150/300"`
- **同义词**: DG附加费 / 危险品附加费 / DG Surcharge / IMO Surcharge / Hazmat Surcharge
- **格式约定**: 通常报价 `20GP/40GP/40HQ` 三个值, 也有按 IMO 分类 (Class 1-9) 分级报价
- **NOR/45 尺 DG**: 极罕见, 如有则塞到「备注」列 (格式 `DG20NOR=...; DG45=...`)
- **v3.8.7 决策 C**: DG 附加费**不依赖运营补救**，Agent 在解析阶段直接填数字字段 + 备注冗余

#### AMS 费用 — Automated Manifest System 美附加费
- **代码 attr**: `ams`
- **飞书 FCL ID**: `fldFjupOqE` (text)
- **必填级别**: P2 (提示)
- **含义**: 美国航线 (US) 的自动舱单系统附加费
- **格式**: 文本 (注意: 当前是 text 不是 number)
- **来源示例**: `"AMS: USD 25"`, `"AMS费 $25/BL"`
- **同义词**: AMS费用 / 美附加费 / AMS Fee / AMS Surcharge / 美国舱单费

#### ENS 费用 — Entry Summary Declaration 欧附加费
- **代码 attr**: `ens`
- **飞书 FCL ID**: `fldQPeXzFU` (text)
- **必填级别**: P2 (提示)
- **含义**: 欧盟 (EU) 的入境摘要声明附加费
- **格式**: 文本
- **来源示例**: `"ENS: EUR 25"`, `"ENS费 €25/BL"`
- **同义词**: ENS费用 / 欧附加费 / ENS Fee / ENS Surcharge / 欧盟入境费

#### 起运区域 — Region of Load (ROL)
- **代码 attr**: `rol`
- **飞书 FCL ID**: `fldawROIDw` (text, v3.7+ 改文本字段)
- **必填级别**: P2 (提示)
- **含义**: 起运港所属区域 (用于统计/筛选)
- **格式**: 文本, 自由填写
- **来源示例**: `"ROL: 东南亚"`, `"起运区域: 东北亚"`, 空白则按 POL 自动推断
- **同义词**: 起运区域 / ROL / Region of Origin / Origin Region
- **v3.7+ 决策**: 字典无对应值时直接写文本, 不做 select option 校验

#### 目的区域 — Region of Discharge (ROD)
- **代码 attr**: `rod`
- **飞书 FCL ID**: `fldSQ3iDGE` (text, v3.7+ 改文本字段)
- **必填级别**: P2 (提示)
- **含义**: 目的港所属区域
- **格式**: 文本
- **来源示例**: `"ROD: 东南亚"`, `"目的区域: 中东"`
- **同义词**: 目的区域 / ROD / Destination Region / Discharge Region

#### 班期 — Frequency / Sailing Schedule
- **代码 attr**: `frequency`
- **飞书 FCL ID**: `fldZKqcjes` (text)
- **必填级别**: P2 (提示)
- **含义**: 船期频率 (每周几班 / 几天一班 / 直航/中转)
- **格式**: 文本, 常见值: `WED` (周三) / `MON` / `DAILY` / `2 days` / `WEEKLY`
- **来源示例**: `"Sailing: Every Wednesday"`, `"班期: 每周三"`
- **同义词**: 班期 / 船期 / Sailing / Frequency / Schedule

#### 直航 — Direct vs Transit
- **代码 attr**: `direct`
- **飞书 FCL ID**: `fldWHV2wHt` (text)
- **必填级别**: OPTIONAL
- **含义**: 该航线是否需要中转
- **格式**: 文本, 常见值: `直航` / `中转` / `Y` / `N` / `T`
- **来源示例**: `"Direct: Y"`, `"直航"`, `"中转"` (parser 映射 Y/T → 直航/中转)
- **同义词**: 直航 / 中转 / Direct / Transit / Non-stop

#### 航程(天) — Transit Time
- **代码 attr**: `tt_days`
- **飞书 FCL ID**: `fld1UUm2YY` (number)
- **必填级别**: OPTIONAL
- **含义**: 从起运港到目的港的运输天数
- **格式**: 整数
- **来源示例**: `"Transit Time: 25 days"`, `"航程 25天"`
- **同义词**: 航程 / 运输天数 / Transit Time / Voyage Days / TT

#### 免柜期(天) — Free Time / Detention Free Days
- **代码 attr**: `free_time`
- **飞书 FCL ID**: `fldXgWrkbW` (number)
- **必填级别**: OPTIONAL
- **含义**: 免箱期 (集装箱免费使用天数)
- **格式**: 整数
- **来源示例**: `"Free Time: 7 days"`, `"免柜期 7天"`
- **同义词**: 免柜期 / 免箱期 / Free Time / Detention Free / Demurrage Free

#### 合约号 — Contract Number
- **代码 attr**: `contract_no`
- **飞书 FCL ID**: `fldNek9BEV` (text)
- **必填级别**: OPTIONAL
- **含义**: 与船公司签订的服务合约编号
- **格式**: 文本
- **来源示例**: `"Contract No: HT-KY-2026-001"`
- **同义词**: 合约号 / 合同号 / Contract No / Service Contract / SCAC

#### 备注 — Remarks / Notes
- **代码 attr**: `remark`
- **飞书 FCL ID**: `fldxZveF4Z` (text)
- **必填级别**: OPTIONAL
- **含义**: 自由文本, 用于记录附加信息 (DG 备注 / 注意事项 / 原始备注)
- **格式**: 文本, 单字段可放多类信息
- **来源示例**: `"备注: DG需提前48小时申请"`, `"Note: Class 9, UN3077"`
- **同义词**: 备注 / Notes / Remarks / Comments

### 8.4 AUTO 字段 (自动填)

| 字段 | 代码 attr | 飞书 FCL ID | 类型 | 自动填规则 |
|---|---|---|---|---|
| 状态 | `status` | `fld5NqEqrn` | text | 完整/P2 提示默认 `已生效`; 缺 P1 字段 → `待补充`；仅允许这两个值 |
| 解析置信度 | `confidence` | `fldfMHmQem` | number 0-1 | LLM 可提供；缺省且结构化校验通过时 writer 自动填 `1.0` |
| 导入时间 | `import_time` | `fldzw7eIC1` | datetime | 写入时按 Asia/Shanghai 时区自动填 ISO 8601 |

### 8.5 备注中要提取的字段 (P2 增强)

业务人员经常把多类信息塞进「备注」列, LLM 解析时需识别以下字段:

| 字段 | 备注中格式示例 | 提取规则 |
|---|---|---|
| NOR O/F (20/40/45 尺) | `"20NOR: USD 850; 40NOR: USD 1600"` | 正则 `(\d+)NOR[::=]\s*(USD\s*)?(\d+)` |
| 45尺 O/F | `"45尺: USD 1800"` / `"45HQ: 1800"` | 正则 `45[尺上下下].*?(\d+)` |
| DG20NOR / DG40NOR / DG45 | `"DG20NOR=200; DG45=300"` | 正则 `DG(\d+(?:NOR|HQ)?)\s*=\s*(\d+)` |
| VAT (税率) | `"VAT: 9%"` / `"增值税 13%"` | 正则 `VAT[:\s]*(\d+)\s*%` |
| 合约号 | `"合约号: HT-2026-001"` | 业务自定义, 看上下文 |
| ETD / ETA | `"ETD: 2026-08-01 ETA: 2026-08-15"` | 短日期格式 |
| AMS / ENS 费用 | `"AMS: USD 25"` / `"ENS: EUR 30"` | 短文本 |
| 船名/航次 | `"Vessel: MSC ANNA V.001W"` | parser 通常单独提取, 备注中可能冗余 |
| 中转港 | `"VIA: SIN"` / `"中转: 香港"` | 看上下文 |
| 直航/中转 | `"直航"` / `"Transit via SIN"` | 同 direct 字段 |
| 超重附加费 | `"OWS: USD 150/TON"` / `"超重 150/吨"` | 短文本 |
| 客户备注 | 任意 | 不解析, 整段保留在「备注」 |

**规则**:
1. 解析后, 检查「备注」中是否含上述格式, 提取并填到对应字段
2. **v3.10.5.1 移除**: 不再在「备注」末尾追加 `[已提取: XXX]` 标记 — LLM 合成该标记是 NO.4921 备注污染根因
3. 客户业务说明 (如危险品审批要求) 留在「备注」中, 不动
4. **v3.10.5.1 新增**: 备注只在原始文件含自由文本时透传; 不要在备注字段写入 `⚠️待补充:`/`(P2 提示)`/`source:`/`(re-import #N)`/`[已提取: XXX]`/`⚠️P0/1/2` 等合成标记 (lark_rate_writer.py v3.10.5.1 已加 regex 过滤, 写到表里仍会被清空)
5. **DG 附加费写入策略（v3.8.7 决策 C，全面）**：解析阶段直接写入 FCL 表的 3 个数字字段 `20GP DG(USD)` / `40GP DG(USD)` / `40HQ DG(USD)`, 同时在「备注」字段保留原文冗余（格式 `DG USD 150/300` 或 `DG USD 150/300/350`），确保运价表数字字段可供后续 Cargoware 导出使用，且业务人员直接看备注也能读懂。

### 8.6 解析常见错误 Top 10

| # | 错误类型 | 现象 | 预防/纠正 |
|---|---|---|---|
| 1 | POL/POD 中文未转 UN/LOCODE | `CNSHA上海` / `曼谷` | 必须调 `port_resolver`, 中文→5 码 |
| 2 | 价格标签互换 | `20GP=1500` 实际是 40HQ 价格 | 解析时按列对齐, 不串列 |
| 3 | 日期格式错 | `2026/7/1` / `7月1日` | 统一转 `yyyy-MM-dd` 零填充 |
| 4 | P/C 漏问 | 解析后 `P/C` 为空 | CRITICAL, 必问业务人员 |
| 5 | 船公司/订舱代理混填 | 把 OOCL 写到订舱代理 | 按角色填对应字段, 备注里船公司信息不重要 (v3.7+) |
| 6 | 币种识别错 | `USD 800` 误识为 `EUR 800` | 看原文币种符号 + 上下文 |
| 7 | DG 附加费位置错 | DG 只写在「备注」, 没填数字字段 | 解析后**同时**填 3 个数字字段 + 备注冗余 (v3.8.7) |
| 8 | 历史默认值 | POL 看到历史都是 CNSHA, 默认填 | D7 硬约束, 必须问业务人员 |
| 9 | 多 sheet 漏读 | Excel 多 sheet 报价, 只读了第 1 个 | 用 `read-xlsx`, 遍历所有 sheet |
| 10 | 行数对账失败 | 文件 49 条, LLM 抽出 45 条 | 对比 `source_summary` + 解析条数, 不一致拒写 |

### 8.7 中英术语速查表

| 中文 | 英文全称 | 英文缩写 | 说明 |
|---|---|---|---|
| 起运港 | Port of Loading | POL | 货物装船港口 |
| 目的港 | Port of Destination | POD | 货物卸船港口 |
| 中转港 | Via Port / Transit Port | VIA | 中转港口 |
| 起运区域 | Region of Load | ROL | 起运港所属区域 |
| 目的区域 | Region of Discharge | ROD | 目的港所属区域 |
| 提货方式 | Pickup / Delivery Method | P/C | CY/CFS/Door 组合 |
| 海运费 | Ocean Freight | O/F | 基础运费 (不含附加费) |
| 危险品附加费 | Dangerous Goods Surcharge | DG / DGS | 危险品集装箱附加费 |
| 美附加费 | Automated Manifest System | AMS | 美国舱单费 |
| 欧附加费 | Entry Summary Declaration | ENS | 欧盟入境摘要费 |
| 免柜期 / 免箱期 | Free Time / Detention Free | FT | 集装箱免费使用天数 |
| 直航 | Direct | D | 不需中转 |
| 中转 | Transit | T | 需中转 |
| 班期 | Frequency / Sailing | - | 船期频率 |
| 航程 | Transit Time | TT | 运输天数 |
| 船公司 | Carrier / Shipping Line | - | 实际承运人 |
| 订舱代理 | Booking Agent | - | 接受订舱的货代 |
| UN/LOCODE | United Nations Location Code | - | 5 位国际港口代码 |
| NVOCC | Non-Vessel Operating Common Carrier | - | 无船承运人 |
| FAK | Freight All Kinds | FAK | 不分货种统一运价 |
| FCL | Full Container Load | FCL | 整柜 |
| LCL | Less than Container Load | LCL | 拼箱 |
| NOR | Non-Operating Reefer | NOR | 非冷藏箱 (即干柜特殊型号) |
| HQ | High Cube | HQ | 高柜 (40' HQ = 40英尺高柜) |
| ETD | Estimated Time of Departure | ETD | 预计离港时间 |
| ETA | Estimated Time of Arrival | ETA | 预计到港时间 |
| UN 编号 | United Nations Number | UN | 危险品编号 (如 UN3077) |
| Class | Hazard Class | - | 危险品等级 (1-9) |
| PG | Packing Group | PG | 包装等级 (I/II/III) |

---

## Excel 持久化解析流程（2026-07-22）

- `read-xls/read-xlsx` 完整提取所有行列到持久化工作区，返回 `parse_id/revision`；`content_markdown` 仅为样本。
- 用 `rate-parse-page` 分页确认表头、数据区和字段列。
- LLM只提交 `rate-field-mapping/v1`；`rate-build-draft` 由 Python从原始 JSONL复制价格并生成行级 provenance。
- 草稿统计以 `source_rows/candidate_rows/valid_entries/skipped_rows` 为准，禁止回复“约 N 条”。
- 业务补充字段调 `dg_rate_query_update_draft` 并带活动 `task_id`；工具自动写回 `confirmed_fields`，不得重读文件或手写新 entries。
- 部分导入先调 `rate-select-draft`，精确数量不一致即停止。
- `/new`、compaction、重启或“继续处理”必须先调 `rate-resume-parse`。
- 所有修改命令必须使用当前 `expected_revision`；`STALE_TASK` 表示旧任务，必须停止并恢复最新状态。

## build_draft 可映射字段清单 (D29, 2026-08-03)

LLM 在 `rate-field-mapping/v1` JSON 中可使用以下字段名（直接写 dataclass 字段名或中文别名）。**必须映射所有 Excel 中存在的字段，不要只映射价格。**

| 字段名 | 中文别名 | Bitable列名 | 说明 |
|---|---|---|---|
| `pol` | POL/起运港 | POL | 起运港5码 |
| `pod` | POD/目的港 | POD | 目的港5码 |
| `pc` | P/C/PC | P/C | CY/Port/Both |
| `carrier` | 船公司 | 船公司 | 船公司名称 |
| `booking_agent` | 订舱代理 | 订舱代理 | 订舱代理名称 |
| `of_20` | 20GP/PRICE20GP | 20GP O/F(USD) | 20GP运价 |
| `of_40` | 40GP/PRICE40GP | 40GP O/F(USD) | 40GP运价 |
| `of_40hq` | 40HQ/PRICE40HQ | 40HQ O/F(USD) | 40HQ运价 |
| `of_20nor` | 20NOR | 20NOR O/F(USD) | 冷代干20GP |
| `of_40nor` | 40NOR | 40NOR O/F(USD) | 冷代干40GP |
| `of_45` | 45尺 | 45尺 O/F(USD) | 45尺运价 |
| `vessel` | 船名/Vessel/VESSEL | 船名 | 船名 |
| `voyage` | 航次/Voyage/VOYAGE | 航次 | 航次 |
| `etd` | ETD/Etd | ETD | 预计离港日 |
| `eta` | ETA/Eta | ETA | 预计到港日 |
| `tt_days` | 航程(天)/航程/T/T/TT/Transit Time | 航程(天) | 航程天数 |
| `direct` | 直航/Direct/DIRECT | 直航 | 直航/中转 |
| `frequency` | 班期/Frequency/FREQUENCY | 班期 | 如 WED/FRI |
| `via_port` | VIA中转港/中转港/Via Port/VIA | VIA中转港 | 中转港5码 |
| `currency` | 币种 | (无独立列) | 默认USD, 写入备注 |
| `dg_20` | 20GP DG(USD)/DG20GP | 20GP DG(USD) | 20GP DG附加费 |
| `dg_40` | 40GP DG(USD)/DG40GP | 40GP DG(USD) | 40GP DG附加费 |
| `dg_40hq` | 40HQ DG(USD)/DG40HQ | 40HQ DG(USD) | 40HQ DG附加费 |
| `contract_no` | 合约号/Contract No/ContractNo | 合约号 | 合约号 |
| `valid_from` | 有效期起 | 有效期起 | 有效期起 |
| `valid_to` | 有效期止 | 有效期止 | 有效期止 |
| `rol` | 起运区域 | 起运区域 | 起运区域 |
| `rod` | 目的区域 | 目的区域 | 目的区域 |
| `free_time` | 免柜期(天)/免柜期/Free Time/FreeTime | 免柜期(天) | 免费用箱天数 |
| `ens` | ENS费用/ENS | ENS费用 | ENS附加费 |
| `ams` | AMS费用/AMS | AMS费用 | AMS附加费 |
| `ows_note` | 超重备注/OWS | 超重备注 | 超重费说明 |
| `remark` | 备注 | 备注 | **表外字段放这里**（SVC代码、DG明细等Bitable没有的字段） |

**映射规则**：

1. Excel 中有的列 → **必须映射**到对应字段名（不要只挑价格映射）
2. Excel 中有但 Bitable 没有对应列的 → 映射到 `remark`（备注）
3. 不确定的列 → 映射到 `remark`，不要丢弃
4. **禁止**把价格数据放到 `remark`（价格已有专属字段如 20GP/40GP/40HQ）
5. 备注字段会被自动清洗（去除纯数字/价格污染、P0/P1/P2 marker 块等），不要依赖备注保存关键数据

**E-003 真实案例 (tier_guide.xlsx)**：Excel 有 16 个 SVC 服务代码 + 船名/航次/ETD，之前的 mapping 只映射了价格和 POD，导致自动识别率仅 43%。按本清单映射后应达 90%+。

### 宽表 pod_groups (D78, 2026-08-27)

**适用场景**: 一行含多个 POD 价格组 (如德翔 Tier Guide: 表头 `G=POD,H=20GP,I=40GP,J=40HQ | K=POD,L=20GP,M=40GP,N=40HQ | ...`, 每行 1-4 个 POD 组)。

**必须配置**: 检测到宽表 (表头重复出现 POD/20GP/40GP/40HQ 组) 时, mapping 的 sheet 加 `pod_groups` 数组, 每组定义该 POD 组的列:

```json
{
  "schema_version": "rate-field-mapping/v1",
  "sheets": {
    "sheet-001": {
      "include": true,
      "header_rows": [14, 15],
      "data_start_row": 16,
      "data_end_row": 146,
      "fields": {
        "carrier": {"column": "A"},
        "vessel": {"column": "B"},
        "voyage": {"column": "C"},
        "etd": {"column": "E"},
        "pc": {"constant": "P"},
        "valid_from": {"constant": "2026-08-04"},
        "valid_to": {"constant": "2026-08-31"}
      },
      "pod_groups": [
        {"pod": "G", "of_20": "H", "of_40": "I", "of_40hq": "J"},
        {"pod": "K", "of_20": "L", "of_40": "M", "of_40hq": "N"},
        {"pod": "O", "of_20": "P", "of_40": "Q", "of_40hq": "R"},
        {"pod": "S", "of_20": "T", "of_40": "U", "of_40hq": "V"}
      ],
      "skip_rules": {"skip_all_price_empty": true}
    }
  }
}
```

**硬约束**:
1. **宽表必须配 pod_groups**, 否则 build_draft 只解析第 1 个 POD 组 (POD1), POD2-4 数据丢失
2. **组内 POD 列为空 → 该组自动跳过** (不生成空 POD 条目); 价格全空 → 跳过 (skip_all_price_empty)
3. **共享字段** (船公司/船名/航次/ETD 等) 只写在 `fields` 一次, 每条 POD 组自动复用
4. **POD 列的值可以是英文名** (如 BANGKOK), build_draft 保留原值, 写库时自动转 UN/LOCODE; 不要手动改
5. **不要手工拼 records 绕过 build_draft** — 配好 pod_groups 后 build_draft 自动展开全部 POD, 走正常 preview/确认流程

### build_draft 报错处理规则 (A1, 2026-08-20)

**背景**: build_draft 的报错信息已附带合法 `FIELD_NAMES` 清单、支持的 transform (`strip`/`upper`/`number`/`rate_number`) 和中文别名提示。

**硬约束**:

1. **报错时先读报错信息里的合法字段清单**, 按清单修正 mapping 后重试; 禁止盲猜字段名 (如自造 `price_20`/`ship_company` 这类清单里不存在的名字)
2. **mapping 的字段名必须来自合法字段清单** (报错信息或本节上表), 不在清单里的字段名一律视为无效
3. **中文别名可用**: 起运区域→`rol`、目的区域→`rod`、船公司→`carrier` 等 (完整对照见本节上表"中文别名"列), 不必死记英文 dataclass 字段名
4. **transform 只用已支持的 4 个**: `strip` / `upper` / `number` / `rate_number`, 禁止自造 transform 名

## ⚠ xls/xlsx 强约束 (2026-07-23, 阻断 38 条价格漏字段事故)

- ❌ 禁止：跳过 `rate-parse-page` + `rate-build-draft`，直接调 `batch-write`。LLM 自己拼 of_20/of_40/of_40hq 等价格字段必漏。
- ❌ 禁止：`dg_rate_query_read_xls` 读 markdown 预览后让 LLM 自己拼 JSON payload。Markdown 上的数字由 LLM 视觉解读不可靠。
- ✅ 必须：xls/xlsx 第一步必须是 `dg_rate_query_read_xls` 创建 parse workspace → `dg_rate_query_parse_page` 分页读 header/字段映射 → LLM 只产出 `rate-field-mapping/v1` JSON → `dg_rate_query_build_draft` 由 Python 从原始 JSONL cell 提取价格 → **`dg_rate_query_batch_preview` 展示给业务人员确认 (D69)** → 业务人员明示确认后 `dg_rate_query_batch_write`。
- ✅ POD 必须经 `dg_rate_query_port_resolve` 校验合法性；解析失败保留中文原名，禁止瞎造 5 字母 UN/LOCODE 占位。

## 🚨 xls/xlsx 字段映射硬约束 (D29 Step 5, 2026-08-03)

**根因**: D29 Step 1-4 部署后实测可可仍只映射价格/POL/POD/P/C/carrier/valid，**漏掉 vessel/voyage/etd/tt_days/direct/frequency 等核心字段**，导致 E-003 跑测 79 条记录中 79 条缺船名/航次/ETD/直航/航程/班期。

**硬约束** (违反任意一条 → build_draft 工具会报错/警告，业务必须驳回):

1. **必须**扫描 Excel 完整列结构, 识别每一列的含义后再产出 mapping JSON
2. **必须**为以下"船舶/航线核心字段"产出 mapping 条目（如果 Excel 中有该列）:
   - `vessel` (船名) — Excel 列名如: 船名/Vessel/VESSEL
   - `voyage` (航次) — Excel 列名如: 航次/Voyage/VOYAGE
   - `etd` (ETD) — Excel 列名如: ETD/Etd
   - `eta` (ETA) — Excel 列名如: ETA/Eta
   - `direct` (直航) — Excel 列名如: 直航/Direct
   - `tt_days` (航程天) — Excel 列名如: 航程/航程(天)/T/T
   - `frequency` (班期) — Excel 列名如: 班期/Frequency
   - `via_port` (中转港) — Excel 列名如: VIA中转港/中转港
3. **禁止**只映射价格字段（20GP/40GP/40HQ）就提交 build_draft
4. **禁止**把"应该有专属字段"的数据塞到 `remark`（如把船名塞备注）
5. **如果** Excel 中有但 Bitable 没有对应列的（如 SVC代码、特殊服务条款），**必须**映射到 `remark` 字段并在回复中告知业务

**build_draft 工具会主动校验**:
- 工具返回 `missing_field_counts` 中 `船名/航次/ETD/直航/航程/班期` 任一非零 → 必须重新确认 Excel 中是否真有这些列
- 如果 Excel 真的没有这些列 → 在 `confirmed_fields_json` 中说明，跳过映射

**反例 (D29 E-003 实际产出, 错误)**:
```json
{"fields": {"20GP": {"column": "F"}, "40GP": {"column": "G"}, "POD": {"column": "A"}, "POL": {"constant": "CNSHA"}, "carrier": {"constant": "兴亚"}, "P/C": {"constant": "CY-CY"}, "valid_from": {"constant": "2026-05-25"}, "valid_to": {"constant": "2026-05-31"}, "currency": {"constant": "USD"}}}
```

**正例 (D29 期望产出, 正确)**:
```json
{"fields": {"20GP": {"column": "F"}, "40GP": {"column": "G"}, "POD": {"column": "A"}, "POL": {"constant": "CNSHA"}, "carrier": {"constant": "兴亚"}, "P/C": {"constant": "CY-CY"}, "valid_from": {"constant": "2026-05-25"}, "valid_to": {"constant": "2026-05-31"}, "currency": {"constant": "USD"}, "vessel": {"column": "E"}, "voyage": {"column": "D"}, "etd": {"column": "F"}, "direct": {"column": "C"}, "tt_days": {"constant": 4, "source": "file_header"}, "frequency": {"column": "B"}}}
```

## 写入飞书硬规则

- **POD**：只用英文 UN/LOCODE 5 码（如 `CNSHA`/`THBKK`），禁止混入中文
- **目的港全称 / 起运港全称** (D70, 2026-08-10)：写库时自动展开为官方英文全名（`上海`→`SHANGHAI`，`曼谷`→`BANGKOK`）。**禁止**用中文简称或含国家后缀的写法（`SHANGHAI, CHINA`）。
- **货币**：number 2 位（如 `1500.00`），单位 USD
- **日期**：`yyyy-MM-dd` 或完整 ISO 8601 `yyyy-MM-dd HH:mm:ss`
- **状态**：只允许 `待补充` / `已生效`；完整记录写入为 `已生效`，P1 缺失写入为 `待补充`，补齐 P1 后自动改为 `已生效`
- **单条/批量写**：分别用 `dg_rate_query_write_record` / `dg_rate_query_batch_write`；两者都必须带活动 `task_id` 与 `confirm_write=true`，文件来源统一上传云盘并填 `原文件附件`；`数据来源` 必须按活动任务的真实文件类型自动确定，禁止采用 LLM payload 中冲突的来源值
- **缺失字段提示**：只保留在工具返回和对业务人员的回复中，不自动拼入「备注」字段

## 🚨 D69 preview→confirm 硬规则 (2026-08-10, 业务方 1v1 反馈)

**根因**: 业务方反馈 "把文件发给可可以后, 他解析出来后直接就入库了, 没有先把数据列出来让业务操作人员先预览, 或要求补充相应的数据"。

**硬约束** (违反任意一条 → 业务方体验崩坏, P0):

1. **必须**先调 `dg_rate_query_batch_preview` 拿到解析+校验结果 (records/abbreviations/p0-p1-p2/dedupe_status)
2. **必须**把 preview 结果完整展示给业务人员 (含: 标准化后 records、简称警告 abbreviations、缺失字段 p0/p1/p2)
3. **禁止**在业务人员明示确认前调 `dg_rate_query_batch_write` 或 `dg_rate_query_write_record` (confirm_write=true)
4. **必须**等业务人员明确表示 "确认/可以写入/入库" 后才可带 `confirm_write=true` 调 batch_write
5. **如业务人员指出缺字段** → 先补字段再重新 preview, 不要直接写
6. **如业务人员拒绝** → 不写入, 回复说明未入库
7. **D77 (2026-08-20) 禁止"两连发自确认"**: batch_write 的 `confirm_write=false` 返回预览数据后, **必须停止本轮, 把预览完整展示给业务人员, 等业务人员回复确认消息**, 才可在下一轮带 `confirm_write=true` 写库。**严禁**在同一轮里 confirm_write=false → confirm_write=true 连续调用自我确认写库 (业务人员必须真实看到并确认预览)。
8. **D77 严格门禁**: 未 preview (task 非 preview_rendered 状态) 时, batch_write/write_record 一律返回 PREVIEW_REQUIRED——必须调 `dg_rate_query_batch_preview` 渲染并展示预览, 不能绕过。

**LLM 回复模板** (preview 后):

```
📋 解析完成, 请确认以下 N 条记录:
[records 摘要 + abbreviations 警告 + 缺失字段]

- ✅ 回复"确认写入" → 我立即入库
- ⚠️ 指出需补充的字段 → 我补充后重新预览
- ❌ 回复"不写入" → 我不入库
```

**技术保障**: plugin `dg_rate_query_batch_write` 描述已强制要求先 preview + 业务人员明示确认; `dg_rate_query_batch_preview` 绝不写 lark (返回 `preview: true` + `next_step` 指导)。

## 🚨 D69-fix preview 全量 + 指纹锁定硬规则 (2026-08-11, 业务方再次反馈)

**根因**: 1v1 重测发现可可 29 次 `batch_write`、0 次 `batch_preview` (绕过预览直接入库), 且 preview 数据 = LLM 手动传 payload — 可可曾只传部分 payload 导致"预览 4 条、写 50 条", 预览不全 + 无确认直接入库。

**硬约束** (违反任意一条 → P0):

1. **preview 必须展示全量待入库数据**: 调用 `dg_rate_query_batch_preview` 时**不要传 payload** (留空 json/records/entries), 让工具自动读 task 全量 draft (`rate-draft-show`); 若必须传 payload, 必须是**全部**待入库记录, 禁止只传部分
2. **入库数据必须与最近一次 preview 完全一致**: `batch_write` 会做指纹校验 (`preview_fingerprint`), payload 与 preview 不一致 → `PREVIEW_PAYLOAD_MISMATCH` 拒绝。**修正数据后必须重新 preview**, 禁止在旧预览上直接改 payload 写入
3. **修正流程**: 业务人员指出缺字段/错误 → `dg_rate_query_update_draft` 补字段 → **重新 `batch_preview` 展示修正后全量** → 业务人员再次明示确认 → 才可 `batch_write`
4. **禁止**在 preview 未成功 (无 `preview_rendered`) 时调 `batch_write` — 会被 `PREVIEW_REQUIRED` 拒绝
5. **禁止**用 `--force` / 绕过 preview 直写; force 仅限手工清理后的重跑

**LLM 回复模板** (修正后重新预览):

```
📋 已按您的要求补充/修正, 以下是修正后的完整 N 条记录:
[修正后全量 records 摘要 + abbreviations + 缺失字段]

- ✅ 回复"确认写入" → 我立即入库 (数据 = 本次预览)
- ⚠️ 仍需补充 → 我再修正并重新预览
```

**技术保障**: `batch_preview` 无 payload 时自动 `rate-draft-show` 读全量 draft; preview 成功后 task 存 `preview_fingerprint + preview_count`; `batch_write` 校验 payload 指纹一致, 不一致拒绝 (防"预览 4 条、写 50 条")。

## 🚨 D80 订舱代理 P0 + 导出问询硬规则 (2026-08-28, CargoWare 导入必填)

**背景**: CargoWare 订舱代理必须先在系统预录入; 解析入库填**订舱代理中文名称** (P0), 导出模板时按订舱口主数据 (assets/booking_agent_master.json, 来自订舱口.xlsx 1304 条) 反查 **CargoWare 代码** 填入。

**解析侧 (P0)**:
1. `build_draft` 自动按 `carrier` 匹配订舱口 → 写 `订舱代理` = 中文名称 (如 兴亚→兴亚船务有限公司); 源文件已有订舱代理则保留
2. **订舱代理是 P0 字段**: 匹配不到 → 该条 `awaiting_user_fields` → **必须问业务人员** (订舱口基本覆盖全部船公司; 匹配不到 = 新船公司/新代理, 需业务确认)
3. **多客商共用代码** (SITC/HAMBURG SUD 等 ambiguous 组): 不自动填 → 问业务确认

**入库后 (每次文件)**: `batch_write` 成功入库后, 回复必须问询是否导出:
```
✅ 本文件已入库 N 条记录。
是否需要按 CargoWare 模板导出这份运价？
- 回复"导出" → 我立即生成模板文件
- 回复"暂不需要" → 本次结束
```

**导出侧**: `export-cw` 自动把 `订舱代理` 中文名称反查为 CargoWare 代码填入模板; 导出结束如出现 `[D80] ⚠️ ... 需人工确认` 清单 (匹配不到 / 多客商共用) → **必须把清单展示给业务人员确认** (哪个代码对 / 补充订舱代理), 确认后再重新导出

## 写工具返回契约（2026-07-21）

`write-record` / `batch-write` 只有同时满足 `code=ok`、`success=true`、`written>=1` 且 `record_id`/`record_ids` 非空时，才能回复写入成功。 插件桥接必须分别传 `--record` / `--records`，禁止把 JSON 作为无标志位置参数。wrapper 已统一从 `WriteResult.write_count` 和 `WriteResult.record_ids` 取值；禁止因 wrapper 返回失败而回退到 raw lark-cli。

## merge/update 字段键（D88, 2026-09-01）

`write_record --merge` / `batch_write merge=true` 的字段 payload 键**中英文均可**：中文字段名（`订舱代理`/`船公司`/`有效期起`）和英文属性名（`booking_agent`/`carrier`/`valid_from`/`pc`/`pol`/`pod`）都会被翻译成飞书 fld ID 写库；`record_id`/`rate_no` 是控制键不参与字段翻译。

## 🚨 无文件更新路径硬规则（D89, 2026-09-01, 三轮打回修复）

**根因**: 三轮复测发现"无文件整批更新"真实链路死锁 — `batch_preview` 代码支持内联 `records/json` payload (提供则无需 parse_id), 但工具 description 没暴露, 可可一直等 parse_id (只来自文件解析) → 无文件 → 死锁。

**硬约束** (违反 → 真实业务链路死锁, P0):

1. **更新已有记录 (带 record_id) 时, 无需文件、无需 parse_id**: 直接传内联 `records`/`json` payload, 每条 = `{record_id: "recvtFK...", 待更新字段: 值}`, 调 `dg_rate_query_batch_preview` → 自动走 **merge 只读预览** (渲染待更新目标, 绝不写库)
2. **预览通过后写库**: `batch_write(confirm_write=true, merge=true, records=同内联 payload)` → 走 **merge 更新**, 原 record_id 更新、**不新建记录**
3. **内联更新也必须先 preview**: 无文件更新同样受 D69/D77 门禁 — 必须先 `batch_preview` 渲染并展示给业务人员确认, 业务人员明示"确认"后才可写; 未 preview → `PREVIEW_REQUIRED`
4. **merge-preview 返回标识**: CLI 返回 `code=preview / mode=merge / message 含「无文件更新预览」` 即为无文件更新预览路径; 展示该预览给业务人员确认后按第 2 条写库
5. **禁止无文件更新走 INSERT**: 更新 payload (含 record_id) 不带 `merge=true` → `RECORD_ID_UPDATE_NOT_SUPPORTED` 拒绝; 先删 record_id 才是新增语义
6. **无文件更新用 task_find 取 task_id, 禁止伪造 source_file 建任务** (D92, 2026-09-02, 四轮打回): 无文件场景 (业务直接在聊天里给核对单/12 条 record_id) 时, 先 `dg_rate_query_task_find` 取当前会话已绑定 task_id 直接复用; 不要 `task_create(source_file="xxx")` 编造文件名 — 文件任务必须对应真实文件, 伪造文件名不会产生 parse_id, 只会让自己再撞 PREVIEW_REQUIRED 死锁
7. **死路报错即走法**: 任何 `PREVIEW_REQUIRED` / `PREVIEW_PAYLOAD_REQUIRED` / `PAYLOAD_REQUIRED` / `RECORD_ID_UPDATE_NOT_SUPPORTED` 报错的 message 里已内置「无文件更新走法」完整步骤 — 报错即指引, 按 message 内 3 步照做即可, 不要再反问"该走哪个工具/参数"

## 🚨 按业务字段自查已有报价（D93, 2026-09-02, 五轮打回修复）

**根因**: 五轮复测发现真实业务中业务员**背不出 record_id** — 规则 6"业务直接给核对单/12 条 record_id"的假设不成立; agent 需要自己按业务字段(尤其订舱代理)定位"哪批记录要更新"。

**硬约束** (违反 → 真实业务走不通, P0):

1. **先自查定位, 不要反问业务要 record_id**: 业务提出"更新某批报价"(如"订舱代理=中外运 的报价改有效期/改 CY")时, 先调 `dg_rate_query_find_records`(只读)按业务字段定位目标集, 得到每条 record_id + pol/pod/carrier/pc/valid_from/valid_to/booking_agent
2. **订舱代理是归一化精确匹配**: `booking_agent` 参数匹配时, 短名(`中外运`)与长法人名(`中外运集装箱运输有限公司`)是**两批不同记录**, 不子串混配 — 需要哪批就传哪批值; 找不到时核对字段值后重查
3. **常用定位**: `booking_agent=中外运` + `valid_on=<今天>`(现有效期窗口) → 得到目标集; 结果**必须展示给业务员逐条核对**(每条含 record_id + POD 等, 防盯错记录, 如苏比克 recvtFK50qq6Li=PHSFS 与 recvtGz18F5tBo=SUBIC 是不同记录)
4. **定位后走内联更新**: 用定位到的 record_id 清单调 `dg_rate_query_batch_preview(task_id, records=[{record_id, 待更新字段}...])` → merge 只读预览 → 展示给业务确认 → `batch_write(confirm_write=true, merge=true, records=同一数组)` → 原 record_id 更新、不新建
5. **export-cw 也可按订舱代理筛**: `dg_rate_query_export_cw` 支持 `booking_agent` 参数(同样归一化精确匹配), 导出 CargoWare 模板前可按订舱代理过滤目标记录

## 写入成功判据（防幻觉 4 步）

唯一允许的"成功"回复模板：

```
✅ 成功导入 N 条运价
# v3.7+: 导入人字段已删除, 不再输出
- batch_no: <batch_no>
- 多维表格: https://ko7vuxlffz.feishu.cn/base/<base_token>?table=<table_id>
```

四步硬约束（LLM 在回复用户前 self-check）：
1. `code:"ok"` + `success:true` 才是真成功
2. `written > 0` 与 entries length 一致
3. **必须** echo N / batch_no / 表格链接三个字段
4. 任一字段缺 → 重调工具，不许脑补

## 🚨 P0 删除保护硬规则 (2026-08-12, 误删 109 条教训)

**根因**: 修正过程用 `delete_record --import-after 2026-08-11` 一刀切删除了当天全部 109 条入库记录 (含 TSL 50 + CUL 46 + 其他), 可可未先 dry-run 核对就实际删除。

**硬约束** (违反 → P0):

1. **宽泛条件禁止实际删除**: 筛选条件只有 `import-after/import-before` (时间范围) 且**未同时指定精确标识** (`rate-no` 或 `record_id`) → 工具拒绝实际删除 (返回 `WIDE_FILTER_BLOCKED`), 只允许 dry-run 列出
2. **删除上限保护**: `would_delete_count > 20` → 强制 dry-run, 即使带 `--yes` 也拒绝 (返回 `DELETE_LIMIT_EXCEEDED`)
3. **删除前必须展示摘要**: 实际删除前, 工具必须打印 `would_delete_count` + 前 5 条 `record_id` 供核对
4. **LLM 行为**: 任何删除操作前, 必须先 dry-run 把将删记录数 + 范围展示给业务人员, 业务人员明示"确认删除"后才可实际删; 禁止自行决定删除范围
5. **修正 ≠ 删除重建**: 修正数据优先用 `write_record --record-id --merge` 更新, 禁止"删了重建"模式 (除非业务方明确要求)

## 🚨 P0 write_record 门禁 + 写后验证硬规则 (2026-08-12)

**根因**: 1v1 测试可可 29 次 batch_write 声称成功但实际 0 条入库 (幻觉); write_record 无 preview 检查 (绕过点); batch_write 成功后无验证机制.

**硬约束**:

1. **write_record 与 batch_write 同等门禁**: `write_record` 更新记录前, task 必须处于 `preview_rendered` 状态; 否则拒绝 (与 batch_write 一致)
2. **修正流程**: update_draft 补字段 → **重新 batch_preview** (展示修正后全量) → 业务方明示确认 → 才可 write_record/batch_write
3. **写后验证**: batch_write 成功后, 工具自动抽查 record-get 验证 `record_ids` 真实存在; 验证失败 → 返回 `WRITE_VERIFY_FAILED`, 禁止报成功
4. **声称成功三要素**: 回复"写入成功"前必须看到工具返回 `success:true` + `written>=1` + `record_ids` 非空; task `written_count` 必须等于声称条数
5. **禁止文本声称**: 任何"XX 条写入成功"的文本, 若无对应工具返回支撑 → 幻觉, 必须重调工具核实

## 文本特殊约定

- `read-txt`："同上" 视为与上一行同列同值；理解前先回看上一行
- "帮我预览但不要入库" → 只调 `read-*` + `preflight`，**不**调 `write-record`
- "同上 OOCL" 之类 → 整段克隆上一行（POL/POD/船公司/Carrier 全复用）

## 异常兜底

- **OCR 失败 / 图片损坏 / 乱码**：问用户重新发，不写库
- **关键字段缺**：不调写工具；若 wrapper 返回 `CRITICAL_FIELDS_MISSING`，逐项问业务人员，补齐后重跑
- **条数对账**：`source_summary` 推算 ≠ LLM 抽出条数 → 拒写并报告"条数对账失败"
- **POL/POD 非法**：`dg-rate-query port-resolve <code>` 验证；不在表内也允许写入（按业务自定义）
- ~~**DG 附加费不规范**：先入库再发"附加费规范化要求"消息给业务人员~~ — **v3.8.7 已废弃**：Agent 解析阶段直接规范化填数字字段 + 备注冗余，不依赖补救


### 🚨 OCR 必走 plugin 硬规则 (P0, 2026-07-24)

收到图片附件时:

- ❌ 禁止：用 LLM native vision (`[agents/tool-images]` 路径) 直接"看"图
- ✅ 必须：调 `dg_rate_query_ocr_image` → 轮询 process 终态 → 用 JSON 结果解析

**触发案例**: 2026-07-24 OCR E2E 10:58:41 日志显示 `[agents/tool-images] Image resized to fit limits: 1859x781px` — 这是 LLM vision 路径, 不是 OCR plugin 路径。

**验证方法**: coco 日志 grep `[agents/tool-images]` → 若出现且任务无 `dg_rate_query_ocr_image` 调用, 即违规。

### 📋 OCR 1v1 完整流程 (D41/D42 后, 2026-08-06)

OCR 路径支持与 xls/xlsx 路径**完整一致的 parse → build → batch_write 流程**：

```
1. dg_rate_query_ocr_image <file>
   ├─ 内部: mineru-open-api OCR → markdown 文本 (D6-5)
   ├─ 内部: 自动创建 parse workspace → 返回 parse_id (OCR 入口 v1)
   └─ 内部: 调用 _parse_weekly_text (D41) 解析 entries (周班航线 5 列价格格式)

2. dg_rate_query_build_draft {task_id, parse_id, expected_revision=0 (OCR 标记), field_mapping}
   ├─ OCR 路径: expected_revision=0 (区别于 xlsx 路径的真实整数 revision)
   └─ Python 从 markdown 提取 price + 自动行级 provenance

3. dg_rate_query_select_draft {task_id}
   └─ 业务方确认 draft

4. dg_rate_query_batch_preview {task_id, json.records}   ← D69 (2026-08-10) 新增
   ├─ 解析+校验但不写库, 返回 records/abbreviations/p0-p1-p2/dedupe_status
   └─ 必须把 preview 结果展示给业务人员, 等业务人员明示确认

5. dg_rate_query_batch_write {task_id, confirm_write=true, json.records}   ← 业务人员确认后才可调用
   └─ 写入飞书表 (per P0 闸门: 缺关键字段自动拒收)
```

**v3.11 OCR 路径关键差异** (vs xlsx 路径):

| 项 | xlsx 路径 | OCR 路径 |
|---|---|---|
| `expected_revision` | 真实正整数 (read_xls 返回) | **`0` (OCR 标记)** |
| parse workspace | read_xls 创建 | ocr_image 自动创建 |
| `read_parse_page` | 调 parse_page 分页读 xlsx JSONL | 不调 (OCR 文本已在 markdown) |
| 字段映射来源 | xlsx 表头 + 字段映射 JSON | markdown 文本 (LLM 解析) |
| 校验闸门 | P0/P1/P2 (lark_rate_writer.py) | **同 P0/P1/P2 (统一闸门)** |

**OCR 错误处理 (v3.11)**:
- ✅ 单条价格空: 整批拒绝 + 提示人工校对 (与 xlsx 路径一致)
- ✅ OCR 完全失败: 不返回空 markdown, 返回明确 `error: "..."` + `ocr_status: failed`
- ✅ 置信度 < 阈值 (默认 0.3): 标记 `low_confidence: true`, 业务方决定是否继续
- ✅ ocr_image 不可用 (mineru-open-api 缺失): 明确错误 "请在 coco 容器内运行"

**OCR 性能优化 (方案一, v3.11+)**:
- `parse_page` 一次返回多页: 1 次调用替代 N 次 (节省 50%+ round-trips)
  - `dg_rate_query_parse_page --all-rows` — 一次返回该 sheet 全部行 (替代分页)
  - `dg_rate_query_parse_page --all-sheets` — 一次返回所有 sheet (替代多次 sheet 切换)
- `batch_write` 批次放大: 50-100 条/批 (vs 当前 8 条/批)
  - 单次 `json.records` 数组可包含 50-100 条 entry, 1 次 LLM round 完成
  - 测试数据 (1v1 testing traj 13-42, 442 records): 平均 10.8s/record → 优化后 ~4-5s/record
  - 极端 case (269s round 含 18 parse_page): 优化后 1 次 all_sheets + 1 次 batch_write ~60-90s
- 跳过已校验 task 的 draft 循环: 已通过 build_draft 的 task 直接 `dg_rate_query_batch_preview` → 业务人员确认 → `batch_write` (D69)
  - 不重复 build_draft → select_draft 步骤
  - 节省约 30% round 数 (按 v3.11 实施数据估算)
  - **注意**: 即使跳过 draft 循环, 也**必须先 preview 给业务人员确认**, 不能直接 batch_write (D69 硬规则)

## Cargoware 模板导出补充


### 🚨 NOR-only 优先修补 (P1, 2026-07-24)

业务人员要求"用 update-record 补一下 NOR-only"或类似修补指令时:

- ❌ 禁止：继续推进其他 batch_write 不做修补
- ✅ 必须：检测到当前任务有 NOR-only 行 (P1 字段缺), 优先调 `dg_rate_query_write_record --merge '{...}'` 修补
- ✅ 必须：修补完成再继续下一轮 batch_write
- ✅ 必须：在最终 reply 中明确"已修补 X 条 NOR-only", 不能再补时说明"原因 + 后续建议"

**触发案例**: 2026-07-24 多 Sheet E2E 中, 业务明确要求修补 NOR-only 3 条 (FREMANTLE/MELBOURNE/BRISBANE), 可可没做而是先继续推 5 个 sheet 写入。

### 代码层检测 (v3.10.6.2, D17)
- `dg_rate_query_build_draft` 返回值新增 `nor_only_count` + `nor_only_records` 字段
- `_detect_nor_only(entry)` 判定: 标准价 (of_20/of_40/of_40hq) 全空 + NOR 价 (of_20nor/of_40nor) 至少一个非空 → NOR-only 行
- 填补建议 (LLM 反问业务后执行): `of_20 ← of_20nor`, `of_40 ← of_40nor`; `of_40hq` / `of_45` 不填补 (NOR 通常无)
- LLM 收到 nor_only_count > 0: 列 NOR-only 行 + 建议填补 → 反问业务 → 确认后调 `dg_rate_query_write_record --merge` 修补 → 修补完再继续 batch_write
- 最终 reply 必须明确 "已修补 X 条 NOR-only"
- 详见: `docs/decisions/20260729-v31062-p1d-nor-only-fill.md`

## write_xls (CargoWare 模板导出, v3.10.6 已修 P0-B + P0-C)

vs `docs/cargoware-templates/FCL_sample(20260506).xls` 模板结构比对 + 实施 (commit `96440b7`):

### 历史问题 (P0-B + P0-C, 2026-07-24 发现, 2026-07-29 修复)
- **P0-B cell TYPE 错位**: 价格/日期列被统一写成 string, Cargoware 导入后无法做数值/日期计算
- **P0-C cell FORMAT 全丢**: 模板黄背景/红字/边框/字体全部丢失, 业务视觉提示归零

### v3.10.6 实现 (D14 + D15)
- `write_xls()` 优先用 `xlutils.copy` 从模板复制 (P0-C 修复), 保留红字/黄背景/边框/字体
- 按列类型分写 cell (P0-B 修复):
  - NUM_COLUMNS (16 列: 20'/40'/40'HC/20'NOR/40'NOR/45'/Reject*/VAT(Cost)/VAT(Sell)/T/T/Free Time) → `float`
  - DATE_COLUMNS (5 列: Valid fm/Valid to/DateTypeEffective/DateTypeExpiration/Closing Date) → `datetime(yyyy-mm-dd)`
  - 其余 35 列 → `str` (POL/POD/Carrier/Booking Agent 等)
- 模板路径: `DEFAULT_TEMPLATE_PATH = "docs/cargoware-templates/FCL_sample(20260506).xls"`, 也可用 `DG_CW_TEMPLATE` 环境变量覆盖
- `xlutils` 在 apt 不可用 → coco start.sh 加 idempotent `pip install --break-system-packages xlutils`
- 模板不可用 / xlutils 缺失 → 自动回退到 `_write_xls_legacy` (兼容老行为)

### 验证
- 单测 426 passed / 8 skipped (新增 `TestWriteXlsTemplate` 5 个: 格式保留 + 数值/日期/文本类型 + 多余行清空)
- 端到端 dev + coco 实导验证: POL=text(1), 20'=number(2), Valid fm=date(3), 模板红字+黄背景完美保留

详见决策: [`docs/decisions/20260729-v3106-p0bc-export-cw-fix.md`](../../docs/decisions/20260729-v3106-p0bc-export-cw-fix.md)。

## 文档索引

| 主题 | 路径 |
|---|---|
| Skill 设计原则 + 接口契约 | [docs/04 §附录 D](../../docs/04-rate-management.md) |
| Skill ↔ Bitable 约束 | [docs/21 §七](../../docs/21-bitable-setup.md) |
| D6 测试用例 | [docs/40 §17.7](../../docs/40-test-plan.md) |
| 数据模型（与 lark_rate_writer.py 同步） | [docs/02-data-model.md](../../docs/02-data-model.md) |
| 部署 / 容器持久化 | [docs/22-container-persistence.md](../../docs/22-container-persistence.md) |
| 架构决策（D1-D17） | [docs/decisions/2026-07-20-architecture-review.md](../../docs/decisions/2026-07-20-architecture-review.md) + [20260727-v310511](../../docs/decisions/20260727-v310511-fixes-and-dg-plugin-lazyload.md) + [20260729-v3106](../../docs/decisions/20260729-v3106-p0bc-export-cw-fix.md) + [20260729-v31061](../../docs/decisions/20260729-v31061-p0a-stop-guard.md) + [20260729-v31062](../../docs/decisions/20260729-v31062-p1d-nor-only-fill.md) |

### 写入后查回闸门 (v3.8.7, 2026-07-22)
**适用范围**：`update-record` / `write-record --merge` / 所有走 `_record_modify` 的路径。
**目的**：防止 `record-upsert` 返回 `ok` 但字段实际未写入的静默 bug。

**机制**：
- `_record_modify` 在 upsert 成功后立即 `record-get` 查回本次更新的字段值
- 用 `_values_equal` 比对：兼容 select list vs str、空值等价、数值 string vs int
- 不一致 → 返回 `WriteResult(success=False, error_msg="写入后查回失败: ...")`
- verify 失败时**不**触发 `auto_resume`

**四步 self-check 补充**：
- 5. 返回值含 `verify_error` 字段 → 写入实际失败，业务人员需重发指令

**性能**：每次 `_record_modify` 多一次 `record-get`，~500ms/条。

### 多 carrier 写入闸门（v3.8.9）
同一文件/批次识别出多个船公司时，若业务人员未明确选择 carrier，必须列出候选并暂停写入；不得从多 carrier 数据中自行挑选一个。只有业务人员明确指定后，才允许按指定 carrier 写入。
## 运价任务连续性规约（2026-07-23）

每个飞书会话同时只允许一个未结束运价任务。任何新运价来源（自然语言、TXT、CSV、XLS/XLSX、图片）到达时，**第一步必须调用 `dg_rate_query_task_open`**，传入当前 OpenClaw session key 和来源文件名：

- `dg_rate_query_task_open`：原子检查当前会话；无活动任务才创建并返回 `task_id`；有活动任务返回 `ACTIVE_TASK_EXISTS`，此时必须停止，不得解析新来源；
- `dg_rate_query_task_find`：仅用于恢复/检查已有任务，不替代新任务的原子 open；
- `dg_rate_query_task_create`：仅用于受控内部创建，不作为普通新文件入口；
- 有活动任务时禁止直接解析新文件，先完成当前任务或让业务员明确废弃。

任务只使用三种业务状态：`进行中`、`待确认`、`已结束`。

- `进行中`：可以继续解析、生成草稿、写入或查回；
- `待确认`：等待补字段或确认入库，不得猜测和写入；
- `已结束`：完成验证或业务员明确废弃，释放会话任务锁。

`/new` 只清理聊天上下文，不释放任务锁。任务信息以 `task.json` 为准，不以历史聊天摘要为准。业务员明确确认后，使用 `dg_rate_query_task_update` 保存确认字段并继续已有 `parse_id/draft_id`；不得重新从历史 Markdown 拼装记录。写入未查回验证成功前，不得回复“已完成”。

## 🚨 D82 解析后先概览再问询 (2026-08-29, 业务方流程反馈)

**根因**: 1v1 测试两轮复现 — 用户上传文件后, 可可"自动 task_open → 立即抛 8 个 P0/P1 字段问题", 用户还没看到解析结果就被连环提问, 体验不佳 (用户原话: "怎么一上来就把任务锁了, 但我没看到 8 个问题")。

**硬约束** (违反 → 流程体验崩坏):

1. **先展示解析概览, 再一次性问询**: task_open 后先完成解析 (read-xls → build_draft), 回复按顺序包含:
   - ① 解析概览: 总条数 + sheet/航线分布 + 识别出的船公司
   - ② 缺字段清单 (P0/P1 缺失, 一次列全, 禁止逐条追问)
   - ③ 推断默认值 (基于当前文件内容, 标注"推断"来源, 让业务一次确认/修正)
2. **禁止先问后给结果**: 未展示解析概览前, 不得抛出字段问题清单
3. **推断默认值规则** (与 D7 不冲突 — 基于当前文件内容, 非历史默认):
   - POL: 表格 TERMINAL 列 WGQ(外高桥)/YS(洋山) 等 → 推断 CNSHA, 标注"从文件推断"
   - carrier: 从 sheet 名/文件名推断 (ONE/IAL/SNL...); 多 carrier → 列候选让业务选 (多 carrier 写入闸门)
   - valid: 从 EFFECTIVE/VALID 列提取区间 (如 9.1-9.14 → valid_from/valid_to)
   - 订舱代理: build_draft 已按 carrier 自动匹配订舱口中文名; 匹配不到 → 列为缺字段
   - 推断值必须标注来源 + 待业务确认, **禁止静默写库**
4. **task_open 提示**: 回复开头说明"已建任务占位 (未写库, written:0), 随时可放弃"
5. **用户可放弃**: 业务说"放弃/不知道这些数据" → task_close abandoned, 不写库, 明确回复"0 条入 FCL"
6. **业务确认后** → batch_preview 全量展示 → 明示确认 → batch_write (D69/D77 门禁不变)

### 建稿参数闸门

`dg_rate_query_build_draft` 只能用于 `read-xls/read-xlsx` 已返回的持久化 `parse_id`，并且必须携带真实正整数 `expected_revision` 和字段映射。普通文本、未解析文件、缺少 revision 时不得调用；收到 `DRAFT_CONTEXT_REQUIRED` 后回到解析或向业务员说明，不得重试 undefined 参数。

### raw lark-cli fallback 规约 (2026-07-23, 防 framework 滥用)

dg-rate-query 的 plugin 工具失败时，**禁止静默 fallback 到 raw lark-cli**。

**硬约束**：

1. 任何 bitable 写操作必须从 dg_rate_query_write_record / dg_rate_query_batch_write 入口；raw feishu_bitable_app_table_record update/create/list 仅在以下条件**同时**满足时使用：
   a. 同一记录类型已至少尝试一次 dg-rate-query 工具，并收到非 code:ok 失败
   b. 用户在最近 1 条消息中明确允许降级（"直接写就行" / "绕过就行" / "用 lark-cli 也可以"）
   c. 在回复中说明：哪个 dg-rate-query 工具失败、错误信息、为什么 fallback、用返回的 record_id 调 record-get 查回验证
2. 任何 bitable 读操作必须从 dg-rate-query 入口；record-search 必须先尝试 dg_rate_query_port_resolve / dg_rate_query_resume_parse 等专用工具
3. 任务状态变更必须从 dg_rate_query_task_update 入口；raw file edit /vol2/.../runtime/rate-tasks/... 仅在确认 task.py 工具失败时用，且要在回复里贴 task_id + 改了什么
4. fallback 后在回复里必须用显眼标记："⚠️ 走了 raw lark-cli，建议工程师查 plugin bug"
5. 同一条对话中 fallback 超过 3 次 → 立即 lock 当前 chat 任务，要求业务人员确认是否需要降级

**正确流程**：
- DG 工具失败 → 报告工具错误（贴 ok=true 但字段未生效的真实情况）
- 询问用户是否降级
- 用户明示同意后（首选）或紧急情况下（事故已发生立即 rollback）
- 用 raw lark-cli 工具 + 验证 + 标记 ⚠️

**禁止**：
- ❌ 工具没失败就 fallback（必须先 tool_call 失败）
- ❌ fallback 后不验证
- ❌ fallback 后不告诉用户
- ❌ 用户说"好了/OK/继续"不算允许降级（必须明确说"用 lark-cli 可以"）

### 飞书 URL 字段查回规约

`原文件附件` 是 URL 样式字段，`record-search --keyword "<文件名或 URL>" --search-field "原文件附件"` 可能稳定返回 0 条；**0 条不能证明记录未写入或已被删除**。

1. 优先使用 `dg_rate_query_batch_write` 返回的 `record_ids` 和工具内置 verify 结果确认写入；需要查单条时，使用已返回的 `record_id` 调 `record-get --record-id <rec_id>`。
2. 必须按关键字查找时，改查可搜索的普通文本字段（优先 `数据来源`，其次 `NO`），再按 `record_id` 查回核对目标字段。
3. ❌ 禁止把 URL 字段 keyword search 的 0 条结果报告为“没有写入”“记录丢失”或据此重复写入。
4. 本规约只解决只读查回问题，不放宽任何 raw lark-cli 写入 fallback 条件。

**任务连续性补充**：
- raw edit index.json 或 task.json 后，必须立即调一次 dg_rate_query_task_find 验证 lock 状态
- raw edit 后才补 dg_rate_query_task_update 写 confirmed_fields 不会触发冲突（task 是按 task_id 索引的）

### task_update 参数补充 (2026-07-23 v2, full schema 解锁)

dg_rate_query_task_update 接受两个等价的字段更新方式：

1. 显式 args：--status 进行中/待确认/已结束, --pending-action 补充字段/确认入库, --updates JSON 字符串
2. LLM 友好的 JSON-only：直接传 {"task_id":"...", "updates_json":"<inner JSON string>"} 或 {"task_id":"...", "updates":"<inner JSON string>"}

**完整可写字段 schema** (updates_json 内层 JSON 支持任意字段, 除保留字段):

| 字段 | 类型 | 说明 | 示例 |
|---|---|---|---|
| `confirmed_fields` | object | 业务已确认的字段值 | `{"POL":"CNSHA","POD":"THBKK"}` |
| `notes` | string | 自由文本备注 | `"业务答复: 跳过红海/美国/THC"` |
| `execution.last_action` | string | 最近一次动作标签 | `"build_draft"` / `"batch_written"` / `"write_verified"` |
| `execution.written_count` | int | 累计写入条数 | `413` |
| `execution.verified_count` | int | 累计查回验证条数 | `413` |
| `execution.last_error` | string\|null | 最近一次错误 | `null` |
| `identifiers.parse_id` | string | 绑定 parse workspace | `"parse_20260723_060712_490f511c_82f35e"` |
| `identifiers.draft_id` | string | 绑定 selection/draft | `"selection_a4c1885429684588"` |
| `source.file_name` | string | 源文件名 | `"yitong---c9c2ecc9.xlsx"` |
| `source.source_sha256` | string | 源文件 sha256 | `"490f511c4c363a6e..."` |
| `pending_questions` | array | 待业务回答的问题 | `[{"field":"POL","status":"resolved"}]` |
| `status` | string | **必须用 --status 显式 arg**, 不要在 updates 里塞 | `"已结束"` |

**保留字段 (不能覆盖)**: `task_id`, `chat_id`, `schema_version`, `created_at`. task_state.py 写时直接 raise TaskStateError.

**plugin 总是 push --updates 到 wrapper**, 即使 updates_json 空字符串也会传 `{}`, **不会** 静默丢弃字段更新.

**v3.10.3 自动推进规则**:
- `read_xls/xlsx`、`build_draft`、`update_draft`、`select_draft`、`write_record/batch_write` 成功后，plugin 自动推进 `task.json`；LLM 不再重复手工写这些中间状态。
- `dg_rate_query_task_update` 只用于：补充业务备注、查回验证后关闭、或明确废弃。`status` 必须用顶层参数，禁止塞进 `updates_json`。
- 查回验证后关闭示例：`status=已结束` + `{"end_reason":"completed","execution":{"last_action":"write_verified","written_count":1,"verified_count":1}}`。

**v3.10.5 dedupe 命中早退** (防 LLM 反复重算 hash):
- `dg_rate_query_batch_write` / `dg_rate_query_write_record` 返回中若含 `duplicate_skipped: true` 或 `code: DUPLICATE_SKIPPED` → 立即向用户回复 `已存在 record_id = XXXX, 不重复入库`, 不要再 exec `python3 -c "import hashlib; ..."` 重算 dedupe key
- ❌ 禁止: dedupe 命中后继续尝试不同参数重写、调用 `lark-cli base +record-search` 反查、或陷入 hash 试算循环 (曾踩坑: 真群 E2E LLM 试算 5+ 分钟)
- 明确废弃示例：`status=已结束` + `{"end_reason":"abandoned"}`。

### v3.10.3 任务状态代码闸门（2026-07-23）

以下参数现在由 plugin schema 和 task_state.py 强制，不再只依赖 LLM 记忆：

1. `dg_rate_query_task_open` 必须先于解析；plugin 自动绑定当前 `chat_id`，并把绝对 `source_file` 规范为 `source.file_name + source.source_path`；不得传 `undefined/null/none`。
2. `dg_rate_query_read_xlsx/read_xls` 必须带活动 `task_id`；`file` 可省略并自动复用 `task.source.source_path`，成功后自动写 `source.* / identifiers.parse_id / execution.last_action`。
3. `dg_rate_query_build_draft` 必须带 `task_id` 与 `confirmed_fields_json`（无业务补充时传 `{}`）；成功后自动推进 draft 状态。
4. `dg_rate_query_update_draft` 必须带 `task_id`；业务补充字段自动合并到草稿并写回 `task.confirmed_fields`。
5. `dg_rate_query_select_draft` 必须带 `task_id`；成功后自动绑定 `selection_id`。
6. `dg_rate_query_write_record` 和 `dg_rate_query_batch_write` 都必须带 `task_id` 与 `confirm_write=true`；file task 自动上传飞书云盘并填 `原文件附件`，成功后自动记录 `written_count/record_ids`。
7. 写后按返回的 `record_id` 调 `record-get` 查回，再用 `task_update --status 已结束` + `end_reason=completed` + `execution.last_action=write_verified` + `verified_count` 关闭；空进度关闭会被拒绝。
8. 业务明确废弃任务时，用 `--status 已结束 --updates '{"end_reason":"abandoned"}'`；`/new` 不释放任务锁。
﻿## 更新现有记录 (v3.10.7, 2026-07-29)

业务人员需要更新已入库的运价记录时，使用 `dg_rate_query_write_record` 工具的 `record_id` 和 `merge` 参数：

### 调用方式
```json
{
  "task_id": "<active_task_id>",
  "confirm_write": true,
  "record_id": "rec_xxx",
  "merge": true,
  "json": "{\"订舱代理\": \"新值\", \"备注\": \"补充信息\"}"
}
```

### 参数说明
- `record_id`: 飞书记录 ID (rec_xxx)，必填
- `merge`: true = 合并模式（只更新提供的字段，保留其他字段）；false 或省略 = 完全覆盖指定字段
- `json`: 要更新的字段 JSON，不需要 `_provenance`

### 使用场景
1. **补充缺失字段**: 业务人员提供之前缺失的信息（如订舱代理、船公司等）
2. **修正错误数据**: 发现入库数据有误需要修正
3. **NOR-only 修补**: 标准价为空但 NOR 价存在时，填补 of_20/of_40

### 返回值
```json
{
  "code": "ok",
  "success": true,
  "written": 1,
  "record_id": "rec_xxx",
  "mode": "merge"
}
```

### 硬约束
- ❌ 禁止：没有 `record_id` 就尝试更新
- ❌ 禁止：用更新模式创建新记录
- ✅ 必须：确认 `record_id` 存在后再调用
- ✅ 必须：更新后验证字段是否真正写入

## 修正标准流程 (B1, 2026-08-20)

业务人员要求修正**已入库**记录时, 走以下标准流程。核心原则: 修正是定点更新, 不是删了重建。

### 正确流程 (5 步)

1. **export 现状**: 用 `dg-rate-query export-cw --from-feishu` 加筛选参数 (`--rate-no` / `--pol` / `--pod` 等) 导出当前记录, 或按已知 `record_id` 调 `record-get`, 确认现状和要改的字段
2. **update_draft**: 调 `dg_rate_query_update_draft` 把修正后的字段写入当前 task 草稿
3. **重新 preview**: 调 `dg_rate_query_batch_preview` 把修正后数据展示给业务人员 (修正后必须重新 preview, 见 D69-fix)
4. **write_record 定点更新**: 业务人员明示确认后, 调 `dg_rate_query_write_record` 带 `record_id` + `merge=true` 更新 (用法见上文 "更新现有记录" 节)
5. **验证**: 按返回的 `record_id` 调 `record-get` 查回核对, 确认字段真正写入后才回复成功

### 硬约束

1. ❌ **禁止"删了重建"**: 修正已入库记录不得用 `delete-record` 删掉再重新写入。`delete-record` 受 P0 删除保护 (`WIDE_FILTER_BLOCKED` 宽泛筛选拦截 / `DELETE_LIMIT_EXCEEDED` 超上限强制 dry-run), 它不是修正工具
2. ✅ **必须用 merge 模式**: `write_record --record-id --merge` 只更新提供的字段, 保留其他字段不动
3. **不要对抗闸门**: `batch_write` 和 `write_record` 都要求 task 先完成 preview, 否则被 `PREVIEW_REQUIRED` 拒绝; 写入成功后工具自动抽查 `record-get` 验证, 失败返回 `WRITE_VERIFY_FAILED`。这些闸门是防幻觉/防误写保护, 遇到报错按流程补 preview 或重新核实, 禁止绕过

## 港口全称字段 (v3.10.8, 2026-07-29)

FCL 海运费表新增两个字段，自动从港口代码解析填充：

| 字段名 | 飞书字段 ID | 说明 |
|---|---|---|
| 起运港全称 | flda1zoW6Z | POL 的英文全称 (如  Shanghai China) |
| 目的港全称 | fldD59VrBG | POD 的英文全称 (如 Bangkok Thailand) |

### 自动填充逻辑
- 写入时，port_resolver.code_to_en_name(code) 自动填充
- 只在字段为空时填充，不覆盖已有值
- 解析失败时保留空值，不阻塞写入

### 使用场景
1. 业务人员查看运价时，无需记忆 UN/LOCODE 5 码
2. 导出到 CargoWare 模板时，可作为参考信息
3. 数据分析和报表展示
﻿## 运价编号查找 record_id (v3.10.9, 2026-07-29)

业务人员可以用运价编号（如 `NO.001`）替代 `record_id` 来更新记录。

### 使用方式
```json
{
  "task_id": "<active_task_id>",
  "confirm_write": true,
  "rate_no": "NO.001",
  "merge": true,
  "json": "{\"订舱代理\": \"克运\"}"
}
```

### 参数说明
- `rate_no`: 运价编号（如 `NO.001`、`NO.002`），自动查找对应的 `record_id`
- 优先级：`record_id` > `rate_no`，两者都有时用 `record_id`
- 查找失败时返回 `RATE_NO_NOT_FOUND` 错误

### 工具调用示例
```
帮我更新运价编号 NO.001 的订舱代理为克运
```
或
```
NO.001 补充订舱代理 = 克运
```
