---
name: moneywiz
description: Convert Alipay/WeChat bill exports to MoneyWiz-compatible CSV with smart category prediction, payee/description simplification, and post-import refund conversion. Triggers on MoneyWiz, 记账, 账单, 导入账单, 退款.
---

# MoneyWiz 账单导入规范

## 功能概览

- **账单导入**：支付宝/微信账单 → MoneyWiz 兼容 CSV，含智能分类预测、商户/描述简化
- **退款转换**：MoneyWiz CSV 不支持 Refund 类型，导入后由脚本将收入（DepositTransaction）转为退款（RefundTransaction），并把分类指回原消费分类、关联原消费交易
- **非人民币账户换算**：账单金额永远是人民币，外币账户的交易在生成 CSV 前换算成账户币种
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

## 实体类型速查

**Z_ENT 编号由 Core Data 模型决定，App 升级迁移时会整体变动，写死必过期。实体名（Z_NAME）不变，认名字不认编号。**

要看当前编号：

```sql
SELECT Z_ENT, Z_NAME FROM Z_PRIMARYKEY ORDER BY Z_ENT;
```

只列出账单导入流程涉及的实体：

| 实体名 (Z_NAME) | 类型 | 关键字段 |
|------|------|----------|
| Account 及其子类<br>（BankSavingAccount、CashAccount、CreditCardAccount、LoanAccount、InvestmentAccount、ForexAccount 等）| 账户 | ZNAME, ZCURRENCYNAME, ZBALLANCE |
| Category | 分类 | ZNAME2, ZPARENTCATEGORY, ZTYPE2（1=支出，2=收入）|
| Payee | 商户 | ZNAME5 |
| DepositTransaction | 收入 | ZDATE1, ZAMOUNT1, ZDESC2, ZACCOUNT2, ZPAYEE2 |
| RefundTransaction | 退款 | ZDATE1, ZAMOUNT1, ZDESC2, ZACCOUNT2, ZPAYEE2 |
| TransferDepositTransaction | 转账收入 | ZDATE1, ZAMOUNT1, ZDESC2, ZACCOUNT2 |
| TransferWithdrawTransaction | 转账支出 | ZDATE1, ZAMOUNT1, ZDESC2, ZACCOUNT2 |
| WithdrawTransaction | 支出 | ZDATE1, ZAMOUNT1, ZDESC2, ZACCOUNT2, ZPAYEE2 |
| WithdrawRefundTransactionLink | 退款关联 | 独立表 ZWITHDRAWREFUNDTRANSACTIONLINK：ZREFUNDTRANSACTION, ZWITHDRAWTRANSACTION |

**金额规则**: 负数 = 支出，正数 = 收入

### 交易类型筛选片段

下面这段直接嵌进 WHERE 就行，不用先查编号再代入，也不写死任何数字。**本文档所有 SQL 示例里的 `Z_ENT IN (...)` 都可以照这个替换**：

```sql
-- 全部交易类型（收入、退款、转账收支、支出）
Z_ENT IN (SELECT Z_ENT FROM Z_PRIMARYKEY WHERE Z_NAME IN (
    'DepositTransaction', 'RefundTransaction', 'TransferDepositTransaction',
    'TransferWithdrawTransaction', 'WithdrawTransaction'))

-- 只要支出
Z_ENT = (SELECT Z_ENT FROM Z_PRIMARYKEY WHERE Z_NAME = 'WithdrawTransaction')
```

**⚠️ 别漏 `RefundTransaction`**，漏了余额算错。

## 常用查询

### 查询账户列表

账户类是多层继承（LoanAccount→CreditCardAccount，ForexAccount→InvestmentAccount），用递归取 `Account` 的全部后代：

```sql
WITH RECURSIVE acct(e) AS (
    SELECT Z_ENT FROM Z_PRIMARYKEY WHERE Z_NAME = 'Account'
    UNION ALL
    SELECT p.Z_ENT FROM Z_PRIMARYKEY p JOIN acct a ON p.Z_SUPER = a.e
)
SELECT Z_PK, ZNAME, ZCURRENCYNAME, ZBALLANCE, ZARCHIVED
FROM ZSYNCOBJECT
WHERE Z_ENT IN (SELECT e FROM acct)
ORDER BY ZARCHIVED, ZNAME;
```

### 计算账户余额

**重要**: `ZBALLANCE` 字段不可靠（经常为 0），实际余额需通过交易记录计算。

公式：`余额 = ZOPENINGBALANCE + SUM(该账户所有交易的 ZAMOUNT1)`

必须包含所有交易类型，不能遗漏 `RefundTransaction`（见「交易类型筛选片段」）。

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
-- 计算所有活跃账户余额（排除 ForexAccount）
WITH RECURSIVE acct(e) AS (
    SELECT Z_ENT FROM Z_PRIMARYKEY WHERE Z_NAME = 'Account'
    UNION ALL
    SELECT p.Z_ENT FROM Z_PRIMARYKEY p JOIN acct a ON p.Z_SUPER = a.e
)
SELECT
    a.Z_PK,
    a.ZNAME as Account,
    a.Z_ENT as Type,
    ROUND(COALESCE(a.ZOPENINGBALANCE, 0) + COALESCE(SUM(t.ZAMOUNT1), 0), 2) as Balance
FROM ZSYNCOBJECT a
LEFT JOIN ZSYNCOBJECT t ON t.ZACCOUNT2 = a.Z_PK AND t.ZAMOUNT1 IS NOT NULL
WHERE a.Z_ENT IN (SELECT e FROM acct)
  AND a.Z_ENT <> (SELECT Z_ENT FROM Z_PRIMARYKEY WHERE Z_NAME = 'ForexAccount')
  AND a.ZARCHIVED = 0
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
WHERE t.Z_ENT IN (SELECT Z_ENT FROM Z_PRIMARYKEY WHERE Z_NAME IN (
        'DepositTransaction','RefundTransaction','TransferDepositTransaction',
        'TransferWithdrawTransaction','WithdrawTransaction')) AND t.ZDATE1 IS NOT NULL
ORDER BY t.ZDATE1 DESC LIMIT 20;
```

### 月度收支统计

```sql
SELECT
    strftime('%Y-%m', datetime(ZDATE1 + 978307200, 'unixepoch', 'localtime')) as Month,
    SUM(CASE WHEN ZAMOUNT1 > 0 THEN ZAMOUNT1 ELSE 0 END) as Income,
    SUM(CASE WHEN ZAMOUNT1 < 0 THEN ABS(ZAMOUNT1) ELSE 0 END) as Expense
FROM ZSYNCOBJECT
WHERE Z_ENT IN (SELECT Z_ENT FROM Z_PRIMARYKEY WHERE Z_NAME IN (
        'DepositTransaction','RefundTransaction','TransferDepositTransaction',
        'TransferWithdrawTransaction','WithdrawTransaction')) AND ZDATE1 IS NOT NULL
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

1. **禁止写入**: 不执行任何 INSERT/UPDATE/DELETE，**唯一例外**：账单导入流程中的 `convert_refunds.py` 可以将刚导入的 DepositTransaction 受控 UPDATE 为 RefundTransaction，按 `category_pk` 受控 UPDATE `ZCATEGORYASSIGMENT` 的分类指向，并按 `original_tx_pk` 受控 INSERT `ZWITHDRAWREFUNDTRANSACTIONLINK` 关联行，必须先关 MoneyWiz + 备份数据库
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

# 用当日汇率重算非人民币账户的金额（详见「非人民币账户」章节）
python3 scripts/apply_live_fx.py <parsed.json> [output.json]
# --rate SGD=5.2824 手工指定汇率；不加则从 open.er-api.com 拉
# 拉不到汇率就保持原样并报错，绝不把人民币数字当外币写进 CSV

# 将 CSV 导入后的退款（DepositTransaction）转为 RefundTransaction
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
  "_note": "非人民币账户的交易还会带 amount_cny / account_currency / fx_rate / fx_rate_source / notes / needs_fx，见「非人民币账户」章节",
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
步骤 1.5: 非人民币账户换算（仅当有 needs_fx=true 时）
┌─────────────────────────────────────────────────────┐
│ 账单金额永远是人民币，但 MoneyWiz 按账户币种记账，    │
│ 且 CSV 导入器没有币种列 —— 换算必须在生成 CSV 前做。 │
│                                                     │
│ parse 阶段已用 config 里的近似汇率换算过，这步用当日 │
│ 汇率重算一遍：                                       │
│ python3 scripts/apply_live_fx.py                    │
│         <parsed.json> <out.json>                    │
│                                                     │
│ 汇率精度不重要，真实扣款要等银行账单。               │
│ 人民币原值保留在 amount_cny 并写进 CSV 备注列。      │
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
│ ⚠️ 每条 is_refund=true 都要溯源原消费，不论           │
│ needs_review 与否：钉 category_pk，并确认原消费       │
│ 真的存在。查不到原消费的先别放过，见「孤立退款」。     │
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
│ needs_fx=true 的条目必须标出换算前后，别只给外币数字：│
│ | 4 | MM/DD| 支付宝 | xxx | 商户D | 分类4 |          │
│ |   ¥13.25 → -2.51 SGD @5.2824 |                    │
│                                                     │
│ 底部显示汇总：                                       │
│ - 总计 N 条，支出 ¥X，收入 ¥Y                        │
│ - 支付宝 M 条，微信 K 条                             │
│ - 含非人民币账户 J 条（汇总按人民币原值计）           │
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
│    python3 scripts/generate_csv.py                  │
│            <confirmed.json> ./bills/processed/      │
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
│ Transfer 三种类型，无法直接生成 RefundTransaction。  │
│ 退款先以 Income 形式导入，再由脚本 UPDATE 转换。      │
│                                                     │
│ ⚠️ 等待用户说「导入完成」后才执行                      │
│                                                     │
│ 1. 检查 confirmed.json 是否含 is_refund=true 条目，  │
│    若无则跳过此步；若有，先确保每条都已钉           │
│    category_pk（原消费分类 Z_PK），否则回补；       │
│    能唯一定位原消费的，一并钉 original_tx_pk        │
│ 2. 运行：                                            │
│    python3 scripts/convert_refunds.py               │
│            <confirmed.json>                         │
│                                                     │
│    脚本自动：                                        │
│    - 关闭 MoneyWiz 进程（osascript quit）            │
│    - 备份数据库到 ./bills/backups/                   │
│    - 对每条 is_refund 交易，按                       │
│      (account, amount, description, ±5min) 严格     │
│      匹配 DepositTransaction 条目，改成 Refund       │
│    - 单条匹配 = 1 才转换；0 或 >1 跳过并报告         │
│    - 转换后按 category_pk 把退款分类指向原消费      │
│      分类对象；未钉/无效 PK 报 unresolved 不动，    │
│      不按名字或金额猜                               │
│    - 钉了 original_tx_pk 的，再往关联表             │
│      ZWITHDRAWREFUNDTRANSACTIONLINK 插一行，        │
│      把退款挂回原消费交易；没钉跳过不猜             │
│    - 删掉导入器克隆出的同名收入分类（严格           │
│      判定：同名+收入树+无引用+无子分类）            │
│                                                     │
│ 3. 输出报告：转换/分类/关联/克隆清理结果            │
│ 4. 提示用户重新打开 MoneyWiz 验证                    │
└─────────────────────────────────────────────────────┘
```

### AI 推测查询

当 `needs_review=true` 时，AI 通过查询 MoneyWiz 历史交易推测分类。

查询同时返回分类名 `Category` 和分类主键 `CategoryPK`。`CategoryPK` 是**退款钉 `category_pk` 的取值来源**（见「退款条目在 JSON 中的特殊字段」）。

```sql
-- 1. 查询相同交易对方的历史分类
SELECT c.ZNAME2 as Category, c.Z_PK as CategoryPK, COUNT(*) as Count
FROM ZSYNCOBJECT t
JOIN ZSYNCOBJECT p ON t.ZPAYEE2 = p.Z_PK
JOIN ZCATEGORYASSIGMENT ca ON t.Z_PK = ca.ZTRANSACTION
JOIN ZSYNCOBJECT c ON ca.ZCATEGORY = c.Z_PK
WHERE t.Z_ENT = (SELECT Z_ENT FROM Z_PRIMARYKEY WHERE Z_NAME='WithdrawTransaction') AND p.ZNAME5 LIKE '%交易对方关键词%'
GROUP BY c.Z_PK ORDER BY Count DESC LIMIT 1;
```

```sql
-- 2. 查询相似描述的历史分类
SELECT c.ZNAME2 as Category, c.Z_PK as CategoryPK, COUNT(*) as Count
FROM ZSYNCOBJECT t
JOIN ZCATEGORYASSIGMENT ca ON t.Z_PK = ca.ZTRANSACTION
JOIN ZSYNCOBJECT c ON ca.ZCATEGORY = c.Z_PK
WHERE t.Z_ENT = (SELECT Z_ENT FROM Z_PRIMARYKEY WHERE Z_NAME='WithdrawTransaction') AND t.ZDESC2 LIKE '%描述关键词%'
GROUP BY c.Z_PK ORDER BY Count DESC LIMIT 1;
```

**⚠️ 退款（is_refund=true）必做**：无论 needs_review 与否，都要跑上面的查询拿到原消费的 `CategoryPK`，写进该退款条目的 `category_pk`。溯源时若能唯一定位到原消费那笔交易，把它的 Z_PK 一并写进 `original_tx_pk`（定位不到唯一一条就不钉，绝不猜）。

### ⚠️ 分类验证（防止幻觉）

**重要**：AI 在确定分类时，如果使用的分类名称不在 `category_rules.json` 的已有映射中，**必须先查询数据库验证该分类是否存在，且收支方向要对**——分类分支出（ZTYPE2=1）和收入（ZTYPE2=2）两棵树，给支出交易配收入分类（或反过来）是错的，光验证名字存在不够。

```sql
-- 验证分类是否存在且方向正确（支出交易要求 ZTYPE2=1，收入交易要求 ZTYPE2=2）
SELECT ZNAME2, ZTYPE2 FROM ZSYNCOBJECT
WHERE Z_ENT = (SELECT Z_ENT FROM Z_PRIMARYKEY WHERE Z_NAME='Category') AND ZNAME2 = '待验证的分类名';
```

如果查询结果为空，说明该分类不存在，需要：
1. 询问用户选择一个有效分类
2. 可以提供相近的分类建议（通过 LIKE 模糊匹配）

```sql
-- 查找相近分类
SELECT ZNAME2 FROM ZSYNCOBJECT
WHERE Z_ENT = (SELECT Z_ENT FROM Z_PRIMARYKEY WHERE Z_NAME='Category') AND ZNAME2 LIKE '%关键词%';
```

### 配置文件

位于项目根的 `config/` 目录。仓库提供 `*.example.json` 模板，复制为不带 `.example` 后缀的版本后填入你的真实配置（已加入 `.gitignore`）：

| 文件 | 用途 |
|------|------|
| `account_map.json` | 支付方式（卡号/支付方式关键词）→ MoneyWiz 账户名；另含 `account_currency`（非人民币账户）和 `fx_fallback_rates`（近似汇率）|
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
| 美团订单-25112211100300001309159849481980 | 美团骑车（外卖订单都带店名，只有单车订单是光秃秃的「美团订单-订单号」）|
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

退款 `RefundTransaction` 在 MoneyWiz 数据模型中**不是独立的交易类型**——它和收入 `DepositTransaction` 在数据库中字段完全相同（账户、金额正数、商户、描述、分类），仅 `Z_ENT` 值不同。MoneyWiz UI 上「右键转为退款」做的就是把 `Z_ENT` 从前者改成后者，并给描述加 ` 的退款` 后缀。

2026-07 模型升级新增 `ZWITHDRAWREFUNDTRANSACTIONLINK` 表（实体名 `WithdrawRefundTransactionLink`）：`ZREFUNDTRANSACTION` 指退款 Z_PK，`ZWITHDRAWTRANSACTION` 指原消费 Z_PK。升级时 MoneyWiz 会给存量退款自动补关联；`convert_refunds.py` 对钉了 `original_tx_pk` 的退款也会插入关联行（升级前的旧模型里退款与原消费没有任何关联字段）。

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
- `category`: 固定 `退款收入`（**仅给 CSV 导入当占位**。退款以 Income 导入，导入器只在收入树里解析分类名，写支出分类名会被克隆成同名的顶级收入分类；真实分类由 `category_pk` 在转换时指回原消费分类对象。收入树里需要有一个「退款收入」分类，没有的话导入时会自动创建一个——它本来就该在收入树，无害）
- `category_pk`: **原消费分类的确切 Z_PK（AI 必填，即使该退款 needs_review=false）**。退款分类必须和原消费用**同一个分类对象**，绝不新建收入分类。分类树里允许重名，光靠分类名选不对，只有钉 Z_PK 才准。取值：AI 推测查询返回的 `CategoryPK`，也就是原消费交易 `ZCATEGORYASSIGMENT.ZCATEGORY` 的实际值。脚本 `convert_refunds.py` **只认 `category_pk`**——没钉或钉了无效 PK 就报 `unresolved` 原样不动，**绝不按分类名或金额猜**（同金额的另一笔无关消费很容易撞上）。
- `original_tx_pk`: 原消费交易的 Z_PK（可选）。AI 溯源时能唯一定位到原消费交易才钉；`convert_refunds.py` 用它往 `ZWITHDRAWREFUNDTRANSACTIONLINK` 插关联行，把退款挂回原消费。没钉就只转类型不建关联，**绝不按金额或描述猜原消费**。

#### ⚠️ 孤立退款（AI 必查，脚本不管）

`parse_alipay.py` 会丢掉「交易状态 == 交易关闭」的支出行，但对应的退款行状态是「退款成功」，不在丢弃之列。**当天下单当天退**的单子就会只剩一笔退款进 JSON，原消费不见了——直接导入等于凭空多一笔收入。

脚本不会修这个，**AI 每次都要自己查**。对每一条 `is_refund=true`：

1. 先在数据库里找原消费（见「AI 推测查询」）。找到 → 正常钉 `category_pk` / `original_tx_pk`。
2. 数据库里找不到，就回原始账单文件里搜同金额的支出行：

```bash
iconv -f GBK -t UTF-8 "<支付宝账单.csv>" | grep "<退款金额>"
```

3. 搜到同金额支出行、且其**交易状态是「交易关闭」**——这就是孤立退款。原消费和退款净额为 0，**两笔都不该导入**，在确认清单里说明原因，请用户拍板剔除。
4. 搜不到、也查不到原消费的，摆给用户问，别自己决定导不导。

判定要抓的是「原消费缺席」这件事，不是某个具体金额或商户。

**事后复核**：剔除对不对，等信用卡账单出来就能验。银行看到的是**收单商户合并后的授权额**，和支付宝逐笔列的商户名、金额都可能对不上——但净额必须一致。拿该卡账户截至账单日的余额和账单本期应还对一下：分毫不差，就证明剔除是对的；差额恰好等于被剔除的退款金额，就说明剔错了。

#### CSV 限制与变通

MoneyWiz CSV 导入器（[官方文档](https://help.wiz.money/en/articles/4440549)）只支持 Income/Withdraw/Transfer 三种字段映射，**不支持** "Type/Refund" 列。所以退款必须先以 Income 导入，再由 `convert_refunds.py` 直接 UPDATE 数据库 `Z_ENT` 字段，按 `category_pk` 把分类指回原消费分类对象，并按 `original_tx_pk` 在 `ZWITHDRAWREFUNDTRANSACTIONLINK` 建立与原消费的关联（这是 skill 唯一允许写库的脚本）。

**⚠️ CSV 占位分类必须用收入树里的「退款收入」**：导入器对 Income 行只认收入分类，写支出分类名会克隆出同名收入分类，污染分类树。`convert_refunds.py` 转换后会删掉这种克隆——但只在严格证明是克隆时才删（与钉回的原分类同名、在收入树、无其他引用、无子分类），像「退款收入」这种正经分类永远不会被删。正确姿势是从源头就不让克隆产生。

#### 多笔退款合并（AI 交互阶段处理）

部分账单（如盒马）一笔订单会拆成多笔小退款。识别条件：同账户 + 同 payee + 同/相近 description + 短时间内（<5min）多次出现。

AI 在交互确认阶段应主动询问：
> 检测到 N 笔同一商户的小额退款（金额合计 X.XX）：
> | 时间 | 描述 | 金额 |
> | ... | ... | ... |
> 是否合并为一条？

合并方案：保留最早一条，金额相加。脚本不自动合并。

### 非人民币账户（跨币种换算）

#### 问题

支付宝/微信账单里的金额**永远是人民币**，但 MoneyWiz 每笔交易存的是**账户自己的币种**。比如一个新加坡元账户，13.25 元人民币的消费直接导进去会被认成 13.25 新元（≈70 人民币），错 5 倍多。

CSV 导入器帮不上忙——[官方文档](https://help.wiz.money/en/articles/4440549)的 12 个可映射字段（Date、Amount、Amount(Expenses)、Amount(Incomes)、Description、Payee、Category、Account、Transfers、Check #、Memo、Tags）里**没有币种列，也没有汇率列**。CSV 里写什么数字，就按账户币种记什么。

**所以换算必须发生在生成 CSV 之前**，写进 CSV 的就是账户币种的金额。

#### MoneyWiz 自己怎么存跨币种

以 CNY 账户上的一笔美元消费为例：

| 字段 | 值 | 含义 |
|------|-----|------|
| `ZAMOUNT1` | -13.426729 | **账户币种金额**，唯一参与余额计算 |
| `ZORIGINALAMOUNT` | -1.99 | 原币金额 |
| `ZORIGINALCURRENCY` | `USD` | 原币种 |
| `ZORIGINALEXCHANGERATE` | 6.7471 | 原币→账户币汇率 |

关系严格成立：`ZAMOUNT1 = ZORIGINALAMOUNT × ZORIGINALEXCHANGERATE`。原币=账户币时汇率为 1.0，两个金额相等。

CSV 只能供给 `ZAMOUNT1`，所以人民币原值改由**备注列**承载。

#### 脚本怎么做

`scripts/fx.py`（被两个 parse 脚本调用）：账户命中 `account_map.json` 的 `account_currency` 就换算，用 `fx_fallback_rates` 里的近似汇率。

```json
"account_currency": { "<你的外币账户>": "SGD" },
"fx_fallback_rates": { "SGD": 5.28, "USD": 6.76, "EUR": 7.8, "HKD": 0.86, "JPY": 0.0428 }
```

换算后交易长这样：

```json
{
  "account": "<你的外币账户>",
  "amount": "-2.51",          // 账户币种(SGD)，写进 CSV
  "amount_cny": "-13.25",     // 人民币原值
  "account_currency": "SGD",
  "fx_rate": 5.2824,
  "fx_rate_source": "live | config_fallback | manual | unresolved",
  "notes": "原价 ¥13.25 @5.2824",   // 进 CSV 备注列
  "needs_fx": true
}
```

**汇率精度不重要**——真实扣款只有银行账单出来才知道，这里给个大概值就行。汇率来源标在 `fx_rate_source`，重复刷新不会叠加备注。

`scripts/apply_live_fx.py` 用当日汇率重算（[open.er-api.com](https://open.er-api.com)，免费无需 key）：

```bash
# 拉实时汇率重算
python3 scripts/apply_live_fx.py parsed.json refreshed.json

# 手工指定汇率（比如已知卡的实际结算价）
python3 scripts/apply_live_fx.py parsed.json --rate SGD=5.2824 out.json
```

拉不到汇率时**保持原样并在 stderr 报告**，绝不把人民币数字当外币放进 CSV。

#### AI 要做的

1. 解析后检查有没有 `needs_fx=true` 的条目，有就跑一次 `apply_live_fx.py` 拿当日汇率
2. 在确认清单里**明确标出**这些条目是换算过的，展示「¥人民币 → 外币 @汇率」，别只给外币数字
3. 新增非人民币账户时，往 `account_currency` 和 `fx_fallback_rates` 里补一条

#### ⚠️ 换算值是占位，库里的金额以用户为准

卡按各自入账日结算，每笔的真实汇率都不同，脚本算的必然对不上，差额在几分钱量级。真实金额只有用户能从卡 app 查到。

**导入后发现库里金额和 CSV 差几分，那是用户按真实账单改的，不是错**——别报成待办，别建议改回估算值。

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
