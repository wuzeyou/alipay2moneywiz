---
name: moneywiz
description: Convert Alipay/WeChat bill exports to MoneyWiz-compatible CSV with smart category prediction, payee/description simplification, and post-import refund conversion. Triggers on MoneyWiz, 记账, 账单, 导入账单, 退款.
---

# MoneyWiz 账单导入规范

## 功能概览

- **账单导入**：支付宝/微信账单 → MoneyWiz 兼容 CSV，含智能分类预测、商户/描述简化
- **退款转换**：MoneyWiz CSV 不支持 Refund 类型，导入后由脚本将 Income (Z_ENT=37) UPDATE 为 Refund (Z_ENT=43)
- **轻量数据查询**：账单导入过程中读取 MoneyWiz 历史数据，用于推测分类、验证账户与分类是否存在

## 前置条件

- **macOS** + 已安装 [MoneyWiz](https://wiz.money/) 桌面版
- **Python 3.6+**，需要 `openpyxl`（处理微信 XLSX）：`pip install openpyxl`
- **已下载并解压**的支付宝/微信账单文件（CSV/XLSX 格式，下载与解压不在本 skill 范围内）
- 已根据 `config/*.example.json` 复制并定制好你的真实配置

## 数据库信息

### 位置与格式

- **路径**: `~/Library/Containers/com.moneywiz.personalfinance/Data/Documents/.AppData/ipadMoneyWiz.sqlite`
- **格式**: Core Data (SQLite)
- **加密**: 无
- **主表**: `ZSYNCOBJECT` - 通过 `Z_ENT` 字段区分实体类型

### 日期转换

MoneyWiz 使用 Core Data 时间戳（从 2001-01-01 起算的秒数）。

```sql
-- 转为可读日期
datetime(ZDATE1 + 978307200, 'unixepoch', 'localtime')

-- 当前时间转 Core Data
strftime('%s', 'now') - 978307200

-- 指定日期转 Core Data
strftime('%s', '2024-01-01') - 978307200
```

## 实体类型速查 (Z_ENT)

只列出账单导入流程涉及的实体：

| Z_ENT | 类型 | 关键字段 |
|-------|------|----------|
| 9-16 | 账户 (Account) | ZNAME, ZCURRENCYNAME, ZBALLANCE |
| 19 | 分类 (Category) | ZNAME2, ZPARENTCATEGORY |
| 28 | 商户 (Payee) | ZNAME5 |
| 37 | 收入 (Deposit) | ZDATE1, ZAMOUNT1, ZDESC2, ZACCOUNT2, ZPAYEE2 |
| 43 | 退款 (RefundTransaction) | ZDATE1, ZAMOUNT1, ZDESC2, ZACCOUNT2, ZPAYEE2 |
| 45 | 转账收入 (TransferDeposit) | ZDATE1, ZAMOUNT1, ZDESC2, ZACCOUNT2 |
| 46 | 转账支出 (TransferWithdraw) | ZDATE1, ZAMOUNT1, ZDESC2, ZACCOUNT2 |
| 47 | 支出 (Withdraw) | ZDATE1, ZAMOUNT1, ZDESC2, ZACCOUNT2, ZPAYEE2 |

**金额规则**: 负数 = 支出，正数 = 收入

**常用交易类型组合**: `Z_ENT IN (37, 43, 45, 46, 47)` - 包含收入、退款、支出、转账

## 常用查询

### 查询账户列表

```sql
SELECT Z_PK, ZNAME, ZCURRENCYNAME, ZBALLANCE, ZARCHIVED
FROM ZSYNCOBJECT
WHERE Z_ENT IN (9,10,11,12,13,14,15,16)
ORDER BY ZARCHIVED, ZNAME;
```

### 计算账户余额

**重要**: `ZBALLANCE` 字段不可靠（经常为 0），实际余额需通过交易记录计算。

公式：`余额 = ZOPENINGBALANCE + SUM(该账户所有交易的 ZAMOUNT1)`

必须包含所有交易类型（Z_ENT IN 37, 43, 45, 46, 47），不能遗漏 43 (RefundTransaction)。

```sql
-- 计算单个账户余额，将 :account_pk 替换为账户 Z_PK
SELECT
    a.ZNAME as Account,
    COALESCE(a.ZOPENINGBALANCE, 0) as OpeningBalance,
    COALESCE(SUM(t.ZAMOUNT1), 0) as TxSum,
    ROUND(COALESCE(a.ZOPENINGBALANCE, 0) + COALESCE(SUM(t.ZAMOUNT1), 0), 2) as Balance
FROM ZSYNCOBJECT a
LEFT JOIN ZSYNCOBJECT t ON t.ZACCOUNT2 = a.Z_PK AND t.ZAMOUNT1 IS NOT NULL
WHERE a.Z_PK = :account_pk;
```

```sql
-- 计算所有活跃账户余额（排除投资账户 Z_ENT=16）
SELECT
    a.Z_PK,
    a.ZNAME as Account,
    a.Z_ENT as Type,
    ROUND(COALESCE(a.ZOPENINGBALANCE, 0) + COALESCE(SUM(t.ZAMOUNT1), 0), 2) as Balance
FROM ZSYNCOBJECT a
LEFT JOIN ZSYNCOBJECT t ON t.ZACCOUNT2 = a.Z_PK AND t.ZAMOUNT1 IS NOT NULL
WHERE a.Z_ENT IN (9,10,11,12,13,14,15) AND a.ZARCHIVED = 0
GROUP BY a.Z_PK
ORDER BY a.ZNAME;
```

### 查询最近交易

```sql
SELECT
    datetime(t.ZDATE1 + 978307200, 'unixepoch', 'localtime') as Date,
    t.ZAMOUNT1 as Amount,
    t.ZDESC2 as Desc,
    a.ZNAME as Account,
    p.ZNAME5 as Payee
FROM ZSYNCOBJECT t
LEFT JOIN ZSYNCOBJECT a ON t.ZACCOUNT2 = a.Z_PK
LEFT JOIN ZSYNCOBJECT p ON t.ZPAYEE2 = p.Z_PK
WHERE t.Z_ENT IN (37, 45, 46, 47) AND t.ZDATE1 IS NOT NULL
ORDER BY t.ZDATE1 DESC LIMIT 20;
```

### 月度收支统计

```sql
SELECT
    strftime('%Y-%m', datetime(ZDATE1 + 978307200, 'unixepoch', 'localtime')) as Month,
    SUM(CASE WHEN ZAMOUNT1 > 0 THEN ZAMOUNT1 ELSE 0 END) as Income,
    SUM(CASE WHEN ZAMOUNT1 < 0 THEN ABS(ZAMOUNT1) ELSE 0 END) as Expense
FROM ZSYNCOBJECT
WHERE Z_ENT IN (37, 43, 45, 46, 47) AND ZDATE1 IS NOT NULL
GROUP BY Month ORDER BY Month DESC LIMIT 12;
```

更多通用查询（账户、最近、月度、分类、商户、搜索）见 `scripts/moneywiz_query.py`，可独立调用。

## 分类关联表

交易与分类的关联存储在 `ZCATEGORYASSIGMENT` 表：

| 字段 | 说明 |
|------|------|
| ZTRANSACTION | 交易 Z_PK |
| ZCATEGORY | 分类 Z_PK |
| ZAMOUNT | 该分类下的金额（支持拆分） |

## Claude 行为规范

### 应该做的

1. **格式化输出**: 查询结果整理为表格，金额保留2位小数
2. **智能汇总**: 大量数据时提供汇总分析
3. **使用脚本**: 复杂查询优先使用 `scripts/` 下的脚本

### 不应该做的

1. **禁止写入**: 不执行任何 INSERT/UPDATE/DELETE，**唯一例外**：账单导入流程中的 `convert_refunds.py` 可以将刚导入的 Income (Z_ENT=37) 受控 UPDATE 为 Refund (Z_ENT=43)，必须先关 MoneyWiz + 备份数据库
2. **隐私保护**: 不主动暴露敏感金额细节

---

## 账单导入功能

### 输入

假设用户已经自行从支付宝/微信导出并解压好账单文件（CSV 或 XLSX），调用本 skill 时直接提供文件路径。下载/解压不在 skill 范围内。

### ⚠️ 重要：脚本与 AI 分工

**脚本负责（自动化）：**
- 解析文件格式（GBK/UTF-8/XLSX）
- 应用静态规则（config 里的映射和预测规则）
- 输出 JSON 中间格式
- 从确认后的 JSON 生成 MoneyWiz CSV

**AI 负责（智能化）：**
- 对 `needs_review=true` 的条目查询数据库推测
- 展示结果并与用户交互确认
- 将用户修改应用到 JSON，调用脚本生成 CSV

### 解析脚本

脚本位置：`scripts/`

```bash
# 主入口（自动识别账单类型，输出 JSON）
python3 scripts/convert_bill.py <账单文件>

# 单独解析支付宝（输出 JSON）
python3 scripts/parse_alipay.py <csv文件>

# 单独解析微信（输出 JSON）
python3 scripts/parse_wechat.py <csv或xlsx文件>

# 从确认后的 JSON 生成 MoneyWiz CSV
python3 scripts/generate_csv.py <confirmed.json> [output_dir]
# 也支持 stdin: cat confirmed.json | python3 generate_csv.py - [output_dir]
# 默认输出到 ./bills/processed/，文件名自动按日期范围生成

# 将 CSV 导入后的退款（Z_ENT=37 Income）转为 Refund (Z_ENT=43)
python3 scripts/convert_refunds.py <confirmed.json> [--dry-run]
# 自动关闭 MoneyWiz、备份数据库到 ./bills/backups/、UPDATE Z_ENT
# --dry-run: 仅报告匹配，不修改数据库
```

### JSON 输出格式

```json
{
  "success": true,
  "source": "alipay",
  "file": "原文件名.csv",
  "transactions": [
    {
      "date": "2024/11/15 12:30:45",
      "description_original": "原始商品说明（冗长）",
      "description": "简化后描述",
      "payee_original": "原始交易对方",
      "payee": "简化后交易对方",
      "category": "预测分类",
      "category_confidence": "high|medium|low|none",
      "account": "映射后账户",
      "transfer_account": "",
      "amount": "-123.45",
      "needs_review": false,
      "is_refund": false
    }
  ],
  "stats": {
    "total": 100,
    "high_confidence": 80,
    "needs_review": 20
  }
}
```

### AI 处理流程

```
步骤 1: 解析用户提供的账单文件
┌─────────────────────────────────────────────────────┐
│ 对每个账单文件运行：                                 │
│ python3 scripts/convert_bill.py <账单文件>            │
│                                                     │
│ 将所有账单的 JSON 结果合并到一个列表                  │
└─────────────────────────────────────────────────────┘
                         ↓
步骤 2: AI 处理 needs_review 条目并与用户交互确认
┌─────────────────────────────────────────────────────┐
│ 对每个 needs_review=true 的交易：                    │
│                                                     │
│ 1. 查询相同交易对方的历史分类                         │
│ 2. 查询相似描述的历史分类                            │
│ 3. 更新 category 字段，标记置信度                    │
│                                                     │
│ 仅展示需要确认的条目：                               │
│ | 日期 | 描述 | 交易对方 | 推测分类 | 金额 |          │
│ |------|------|----------|----------|------|          │
│ | MM/DD| 商品 | 商户A | ? 分类 | -XX.XX |             │
│                                                     │
│ 置信度标记：                                         │
│ ? medium  - 历史推测，建议确认                       │
│ ✗ none    - 无法推测，必须指定                       │
│                                                     │
│ ⚠️ 必须等用户确认所有疑问条目后才能进入下一步         │
└─────────────────────────────────────────────────────┘
                         ↓
步骤 3: 展示完整交易清单请求最终确认
┌─────────────────────────────────────────────────────┐
│ ⚠️ 所有疑问解决后，展示【全部】交易条目               │
│                                                     │
│ 展示格式（按日期倒序排列）：                          │
│ | # | 日期 | 来源 | 描述 | 交易对方 | 分类 | 金额 |   │
│ |---|------|------|------|----------|------|------|   │
│ | 1 | MM/DD| 支付宝 | xxx | 商户A | 分类1 | -XX.XX |  │
│ | 2 | MM/DD| 微信  | xxx | 商户B | 分类2 | -XX.XX |  │
│ | 3 | MM/DD| 支付宝 | xxx | 商户C | 分类3 | -XX.XX |  │
│ ...                                                 │
│                                                     │
│ 底部显示汇总：                                       │
│ - 总计 N 条，支出 ¥X，收入 ¥Y                        │
│ - 支付宝 M 条，微信 K 条                             │
│                                                     │
│ 询问：「请确认以上交易，或告诉我需要修改哪些？」       │
│                                                     │
│ ⚠️ 必须等用户明确确认后才能生成 CSV                   │
└─────────────────────────────────────────────────────┘
                         ↓
步骤 4: 用户确认后生成合并 CSV
┌─────────────────────────────────────────────────────┐
│ 1. 将用户修改应用到交易 JSON 数据                     │
│ 2. 保存为临时 JSON 文件                              │
│ 3. 调用脚本生成 CSV：                                │
│    python3 scripts/       │
│            generate_csv.py <confirmed.json>          │
│            ./bills/processed/                        │
│                                                     │
│ ⚠️ 必须用脚本生成，禁止 AI 手写 CSV 生成代码          │
│                                                     │
│ 脚本自动处理：                                       │
│ - 文件名：bills_YYYYMMDD-YYYYMMDD.csv               │
│ - 列顺序：账户,转账,描述,交易对方,分类,日期,备注,标签,金额│
│ - 日期格式：YYYY/MM/DD HH:MM:SS（MoneyWiz 兼容）    │
│ - 按日期倒序排列                                     │
│                                                     │
│ 生成后自动打开 MoneyWiz 导入：                        │
│ open -a MoneyWiz ./bills/processed/bills_xxx.csv    │
│ MoneyWiz 会自动进入导入界面，等待用户完成导入          │
└─────────────────────────────────────────────────────┘
                         ↓
步骤 4.5: 转换退款（仅当 confirmed.json 含 is_refund 时）
┌─────────────────────────────────────────────────────┐
│ 背景：MoneyWiz CSV 导入器仅支持 Income/Withdraw/      │
│ Transfer 三种类型，无法直接生成 Refund (Z_ENT=43)。  │
│ 退款先以 Income 形式导入，再由脚本 UPDATE 转换。      │
│                                                     │
│ ⚠️ 等待用户说「导入完成」后才执行                      │
│                                                     │
│ 1. 检查 confirmed.json 是否含 is_refund=true 条目，  │
│    若无则跳过此步                                    │
│ 2. 运行：                                            │
│    python3 scripts/       │
│            convert_refunds.py <confirmed.json>       │
│                                                     │
│    脚本自动：                                        │
│    - 关闭 MoneyWiz 进程（osascript quit）            │
│    - 备份数据库到 ./bills/backups/                   │
│    - 对每条 is_refund 交易，按                       │
│      (account, amount, description, ±5min) 严格     │
│      匹配 Z_ENT=37 的 Income 条目，UPDATE 为 43      │
│    - 单条匹配 = 1 才转换；0 或 >1 跳过并报告         │
│                                                     │
│ 3. 输出报告：成功/失败明细                           │
│ 4. 提示用户重新打开 MoneyWiz 验证                    │
└─────────────────────────────────────────────────────┘
```

### AI 推测查询

当 `needs_review=true` 时，AI 通过查询 MoneyWiz 历史交易推测分类：

```sql
-- 1. 查询相同交易对方的历史分类
SELECT c.ZNAME2 as Category, COUNT(*) as Count
FROM ZSYNCOBJECT t
JOIN ZSYNCOBJECT p ON t.ZPAYEE2 = p.Z_PK
JOIN ZCATEGORYASSIGMENT ca ON t.Z_PK = ca.ZTRANSACTION
JOIN ZSYNCOBJECT c ON ca.ZCATEGORY = c.Z_PK
WHERE t.Z_ENT = 47 AND p.ZNAME5 LIKE '%交易对方关键词%'
GROUP BY c.Z_PK ORDER BY Count DESC LIMIT 1;
```

```sql
-- 2. 查询相似描述的历史分类
SELECT c.ZNAME2 as Category, COUNT(*) as Count
FROM ZSYNCOBJECT t
JOIN ZCATEGORYASSIGMENT ca ON t.Z_PK = ca.ZTRANSACTION
JOIN ZSYNCOBJECT c ON ca.ZCATEGORY = c.Z_PK
WHERE t.Z_ENT = 47 AND t.ZDESC2 LIKE '%描述关键词%'
GROUP BY c.Z_PK ORDER BY Count DESC LIMIT 1;
```

### ⚠️ 分类验证（防止幻觉）

**重要**：AI 在确定分类时，如果使用的分类名称不在 `category_rules.json` 的已有映射中，**必须先查询数据库验证该分类是否存在**。

```sql
-- 验证分类是否存在
SELECT ZNAME2 FROM ZSYNCOBJECT
WHERE Z_ENT = 19 AND ZNAME2 = '待验证的分类名';
```

如果查询结果为空，说明该分类不存在，需要：
1. 询问用户选择一个有效分类
2. 可以提供相近的分类建议（通过 LIKE 模糊匹配）

```sql
-- 查找相近分类
SELECT ZNAME2 FROM ZSYNCOBJECT
WHERE Z_ENT = 19 AND ZNAME2 LIKE '%关键词%';
```

### 配置文件

位于项目根的 `config/` 目录。仓库提供 `*.example.json` 模板，复制为不带 `.example` 后缀的版本后填入你的真实配置（已加入 `.gitignore`）：

| 文件 | 用途 |
|------|------|
| `account_map.json` | 支付方式（卡号/支付方式关键词）→ MoneyWiz 账户名 |
| `category_rules.json` | 交易对方/关键词 → 分类 |
| `payee_rules.json` | 交易对方名称简化 |
| `description_rules.json` | 交易描述简化规则 |

### 描述简化规则 (description_rules.json)

自动去除交易描述中的冗余信息：

```json
{
  "remove_patterns": [
    "-\\d{20,}",           // 去掉长订单号
    "-美团App.*$",         // 去掉美团后缀
    "（[^）]*店）"          // 去掉括号内店铺地址
  ],
  "replacements": {
    "GULOOO BURGER鲜制牛肉汉堡[^\\s]*": "GULOOO BURGER",
    "瑞幸咖啡[（\\(][^）\\)]*[）\\)]": "瑞幸咖啡"
  },
  "simplify_suffixes": ["等多件$", "等\\d+类商品$"],
  "keep_prefixes": ["退款-", "转账备注:"]
}
```

**简化效果示例**：
| 原始描述 | 简化后 |
|----------|--------|
| 美团订单-25112211100300001309159849481980 | 美团订单 |
| 张亮麻辣烫·麻辣拌（浙江中路店）-美团App-xxx | 张亮麻辣烫 |
| GULOOO BURGER鲜制牛肉汉堡(香港广场店)外卖订单 | GULOOO BURGER |
| 盒马 魔芋丝结 200g等3类商品 | 盒马 魔芋丝结 200g |

### 文件路径规范

| 用途 | 路径 |
|------|------|
| 下载账单 | `./bills/downloads/` |
| 生成 CSV | `./bills/processed/` |

**禁止**放到 `~/Downloads/` 或其他随意位置。

| 用途 | 路径 |
|------|------|
| 数据库备份（退款转换前自动生成） | `./bills/backups/` |

### 退款处理

#### 退款的本质

退款 (RefundTransaction, Z_ENT=43) 在 MoneyWiz 数据模型中**不是独立的交易类型**——它和 Income (Deposit, Z_ENT=37) 在数据库中字段完全相同（账户、金额正数、商户、描述、分类），仅 `Z_ENT` 值不同。MoneyWiz UI 上「右键转为退款」做的就是把 `Z_ENT` 从 37 改成 43，并给描述加 ` 的退款` 后缀。

数据库中退款**不指向**原始消费交易（无关联字段），仅靠商户/分类便于报表区分。

#### 脚本识别规则

| 来源 | 判定条件 |
|------|----------|
| 支付宝 | `交易分类 == "退款"` 或（`商品说明` 以 `退款-` 开头 且 `收/支 == "不计收支"`）|
| 微信 | `交易类型` 含 `-退款` 或以 `退款` 结尾 |

退款条目在 JSON 中的特殊字段：
- `is_refund: true`（仅退款条目设置）
- `amount`: 正数
- `transfer_account`: 空（**不是转账**）
- `description`: `<原描述简化后> 的退款`
- `category`: 推测原消费分类（用于 Refund 视图归类）

#### CSV 限制与变通

MoneyWiz CSV 导入器（[官方文档](https://help.wiz.money/en/articles/4440549)）只支持 Income/Withdraw/Transfer 三种字段映射，**不支持** "Type/Refund" 列。所以退款必须先以 Income 导入，再由 `convert_refunds.py` 直接 UPDATE 数据库 `Z_ENT` 字段（这是 skill 唯一允许写库的脚本）。

#### 多笔退款合并（AI 交互阶段处理）

部分账单（如盒马）一笔订单会拆成多笔小退款。识别条件：同账户 + 同 payee + 同/相近 description + 短时间内（<5min）多次出现。

AI 在交互确认阶段应主动询问：
> 检测到 N 笔同一商户的小额退款（金额合计 X.XX）：
> | 时间 | 描述 | 金额 |
> | ... | ... | ... |
> 是否合并为一条？

合并方案：保留最早一条，金额相加。脚本不自动合并。

### 规则学习（自主进化机制）

Skill 应具备从用户反馈中学习的能力，逐步完善规则配置。

#### 触发时机

**1. 分类被修改时**
```
场景：用户指定 "新商户A" 的分类为 "礼物"

AI 询问：
> 要把「新商户A → 礼物」加入分类规则吗？

用户确认 → 更新 category_rules.json 的 payee_category
```

**2. 描述未被有效简化时**
```
场景：发现多条描述仍然冗长，且有相似模式

AI 分析后建议：
> 发现以下描述模式可以简化：
> - "xxx（xxx店）-美团App-xxx" → 建议添加规则去除店铺和订单号
> 要添加这条简化规则吗？

用户确认 → 更新 description_rules.json
```

**3. 新交易对方名称冗长时**
```
场景：交易对方为 "上海某某餐饮管理有限公司"

AI 询问：
> "上海某某餐饮管理有限公司" 要简化为什么名称？

用户回答 "某某餐饮" → 更新 payee_rules.json
```

**4. 账单处理完成后**
```
回顾本次处理中用户的修改，汇总建议：

> 本次处理中发现以下可学习的规则：
> 1. 分类：新商户B → 日用百货
> 2. 交易对方简化：上海xxx有限公司 → xxx
>
> 要将这些规则加入配置吗？（可选择部分）
```

#### 更新配置文件的方式

**category_rules.json** - 添加 payee_category 映射：
```json
{
  "payee_category": {
    "新商户A": "礼物",
    "新商户B": "日用百货"
  }
}
```

**description_rules.json** - 添加 remove_patterns 或 replacements：
```json
{
  "remove_patterns": ["新的正则模式"],
  "replacements": {"原始模式": "简化后"}
}
```

**payee_rules.json** - 添加 simplify 映射：
```json
{
  "simplify": {
    "上海某某餐饮管理有限公司": "某某餐饮"
  }
}
```

#### 学习原则

1. **始终询问**：修改配置前必须获得用户确认
2. **解释原因**：说明为什么建议添加这条规则
3. **可选择性**：用户可以选择部分规则，或全部拒绝
4. **不重复**：已存在的规则不再询问
5. **特例排除**：用户明确说"这是特例"时，不建议加入规则

### 使用示例

**示例 1：处理多个账单（支付宝+微信）**
```
用户: 帮我转换这两个账单文件

AI: [运行脚本解析所有账单]

    解析完成：
    - 支付宝：N 条交易
    - 微信：M 条交易
    - 共 N+M 条，其中 K 条需要确认

    [查询数据库推测分类...]

    以下交易需要确认：
    | 日期 | 来源 | 描述 | 交易对方 | 推测分类 | 金额 |
    |------|------|------|----------|----------|------|
    | MM/DD | 支付宝 | 商品1 | 商户A | ? 分类1 | -XX.XX |
    | MM/DD | 支付宝 | 商品2 | 商户B | ? 分类2 | -XX.XX |
    | MM/DD | 支付宝 | 二手转让 | 朋友X | ? 收入 | +XX.XX |
    | MM/DD | 微信 | 转账 | 朋友Y | ? 红包收入 | +XX.XX |

用户: 前两条没问题，第三条是3C回收，第四条是红包收入

AI: 好的，已更新。以下是完整交易清单，请确认：

    | # | 日期 | 来源 | 描述 | 交易对方 | 分类 | 金额 |
    |---|------|------|------|----------|------|------|
    | 1 | MM/DD | 支付宝 | 退款-XXX | 商户A | (转账) | +XX.XX |
    | 2 | MM/DD | 微信  | 通行扣费 | 高速ETC | 高速过路费 | -XX.XX |
    | 3 | MM/DD | 支付宝 | 外卖订单 | 美团 | 一人食 | -XX.XX |
    | ... |

    **汇总**：共 N+M 条，支出 ¥XXX.XX，收入 ¥XX.XX
    （支付宝 N 条，微信 M 条）

    请确认以上交易，或告诉我需要修改哪些？

用户: 确认

AI: CSV 已生成：./bills/processed/bills_YYYYMMDD-YYYYMMDD.csv
    共 N+M 条交易已合并导出。

    ---
    📚 **规则学习建议**

    本次处理中发现以下可学习的规则：
    1. 分类：商户A → 分类1
    2. 分类：商户B → 分类2

    要将这些规则加入配置吗？

用户: 这两个商户名太模糊了，不加

AI: 好的，已跳过。下次遇到类似商户仍会询问确认。
```

**示例 2：处理单个账单**
```
用户: 帮我转换 ~/Downloads/alipay_20241115.csv

AI: [运行脚本]
    解析完成，共 45 条交易，其中 2 条需要确认。

    以下交易需要确认：
    | 日期 | 描述 | 交易对方 | 推测分类 | 金额 |
    |------|------|----------|----------|------|
    | 11/15 | 新商户消费 | 新商户A | ? 一人食 | -28.00 |
    | 11/14 | 某商品购买 | 新商户B | ✗ 无法推测 | -150.00 |

用户: 新商户A 是一人食没错，新商户B 是"礼物"

AI: 好的，已更新。以下是完整交易清单，请确认：

    | # | 日期 | 描述 | 交易对方 | 分类 | 金额 |
    |---|------|------|----------|------|------|
    | 1 | 11/15 | xxx | 美团 | 一人食 | -16.80 |
    | 2 | 11/15 | 新商户消费 | 新商户A | 一人食 | -28.00 |
    | ... |
    | 45 | 11/01 | 某商品购买 | 新商户B | 礼物 | -150.00 |

    **汇总**：共 45 条，支出 ¥1,234.56，收入 ¥200.00

    请确认以上交易，或告诉我需要修改哪些？

用户: 确认

AI: CSV 已生成：./bills/processed/bills_20241101-20241115.csv

    ---
    📚 **规则学习建议**

    本次处理中发现以下可学习的规则：
    1. 分类：新商户B → 礼物

    要将这条规则加入配置吗？

用户: 加

AI: ✅ 已更新 category_rules.json
    新增规则：新商户B → 礼物

    下次遇到「新商户B」将自动归类为「礼物」。
```
