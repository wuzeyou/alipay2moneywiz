# alipay2moneywiz

> 将支付宝、微信账单导入 MoneyWiz 的 [Claude Code Skill](https://docs.claude.com/en/docs/claude-code)。

支持账户映射、智能分类预测、商户/描述简化、退款类型自动转换。

---

## v1 → v2 转型说明

旧版（v1）是 Node.js CLI 脚本（`npm run alipay` / `npm run wechat`），完整代码保留在 [`archive/`](./archive/) 目录，仍可独立使用。

v2 完全用 Python 重写，并以 Claude Code Skill 的形式发布——AI 在对话中驱动整个流程，包括分类预测、与你交互确认、生成 CSV、自动转换退款类型，全部一气呵成。

如果你 Star 过旧版本：感谢一路同行。新版本是同一个目标的更聪明的实现。

---

## ⚠️ 这是简化版本

为了让仓库可公开，我从我自用的 skill 中**移除了三块功能**：

1. **从 Gmail 自动下载账单邮件** —— 依赖私有的 `gmail-reader` skill，未开源。本版本要求你自己从支付宝/微信 App 导出账单（CSV/XLSX），解压后把文件交给 AI。
2. **完整的 MoneyWiz 数据库查询** —— 我自用版可以查账户余额、月度收支、分类统计、商户消费、加密货币持仓等。本版本仅保留账单导入流程必要的查询（推测分类用）。
3. **信用卡邮件对账** —— 我自用版能在导入完成后，从 Gmail 拉各家银行的电子账单邮件，与 MoneyWiz 余额逐卡核对。本版本完全省略。

> 💡 **如果你想要更完善的版本**：这个 Skill 本身就是一份 Claude Skill 写法的范例。你完全可以让 AI 阅读 [`SKILL.md`](./SKILL.md) 和 [`scripts/`](./scripts/)，然后让它帮你扩展上面任意一项——只要你愿意把 Gmail OAuth、银行邮件正则等个人化部分自己接进去。Claude Skill 的好处就是改它本身比改一个传统 CLI 脚本容易得多。

---

## 系统要求

- macOS（MoneyWiz 数据库路径是 macOS 专属的 Container 路径）
- 已安装 [MoneyWiz](https://wiz.money/) 桌面版
- Python 3.6+，需要 `openpyxl`（处理微信 XLSX）：`pip install openpyxl`
- [Claude Code](https://docs.claude.com/en/docs/claude-code)

## 安装

```bash
# 1. clone 到 Claude skills 目录
git clone https://github.com/wuzeyou/alipay2moneywiz.git ~/.claude/skills/moneywiz

# 2. 复制配置模板，填入你的真实账户/规则
cd ~/.claude/skills/moneywiz/config
for f in *.example.json; do cp "$f" "${f%.example.json}.json"; done
# 然后编辑 account_map.json 等
```

`config/*.json`（不带 `.example`）已加入 `.gitignore`，不会被误推。

## 使用

在 Claude Code 中，直接说：

```
帮我导入这个支付宝账单：~/Downloads/支付宝交易明细.csv
```

或同时导入支付宝和微信：

```
帮我导入：
- 支付宝：~/Downloads/支付宝交易明细.csv
- 微信：~/Downloads/微信账单.xlsx
```

AI 会按 [`SKILL.md`](./SKILL.md) 的协议执行：解析 → 推测分类 → 与你交互确认每一条 → 生成 MoneyWiz CSV → 打开 MoneyWiz 完成导入 → 把退款 (Income) UPDATE 为 Refund 类型。

### 直接调用脚本

不走 AI 也可以：

```bash
python3 scripts/convert_bill.py <账单文件>           # 解析为 JSON
python3 scripts/generate_csv.py confirmed.json      # 生成 CSV
python3 scripts/convert_refunds.py confirmed.json   # 退款转换（写库前会备份）
python3 scripts/moneywiz_query.py recent 30         # 查询历史交易
```

## 项目结构

```
alipay2moneywiz/
├── SKILL.md                # Claude Skill 协议（AI 按此执行）
├── scripts/                # Python 脚本（解析、CSV 生成、退款转换、查询）
├── config/                 # 配置模板（*.example.json）+ 你的实际配置（gitignored）
├── archive/                # v1 Node.js 旧版（仍可用）
└── bills/                  # 工作目录（gitignored，账单/CSV/数据库备份）
```

## 退款处理（核心特性）

MoneyWiz 的 CSV 导入器仅支持 Income / Withdraw / Transfer 三种类型，**无法直接生成 Refund (Z_ENT=43)**。本 Skill 的处理方式：

1. 解析阶段识别退款（支付宝 `交易分类=退款`、微信 `交易类型` 含 `-退款`），在 JSON 标记 `is_refund: true`，CSV 写为 Income。
2. 你在 MoneyWiz 完成 CSV 导入。
3. 运行 `convert_refunds.py`，自动关闭 MoneyWiz、备份数据库到 `./bills/backups/`、把刚导入的 Income 条目按严格匹配（账户 + 金额 + 描述 + ±5min 时间窗口）UPDATE 为 Refund。

Refund (Z_ENT=43) 与 Income (Z_ENT=37) 在数据库中字段完全一致，仅 `Z_ENT` 标记不同——本 Skill 利用了这个事实，绕过 CSV 导入器的限制。

## 数据库安全

- MoneyWiz SQLite 是明文未加密的（`~/Library/Containers/com.moneywiz.personalfinance/...`）。
- 大部分脚本是只读的。
- **唯一写库的脚本** 是 `convert_refunds.py`，它的安全机制：
  - 写之前自动 `osascript` 关闭 MoneyWiz 进程（避免运行时损坏）
  - 写之前把整个 SQLite 拷贝一份到 `./bills/backups/<时间戳>.sqlite`
  - 单事务 `BEGIN IMMEDIATE; ... COMMIT`，异常自动 `ROLLBACK`
  - WHERE 条件严格（账户 PK + 金额误差 0.001 + 描述完全相等 + 时间窗口 5min + 非转账）
  - 单条匹配数量必须 = 1 才提交，0 或多条都会报告并跳过
  - 支持 `--dry-run` 仅查询不修改

## 限制

- 仅 macOS（数据库路径硬编码 macOS Container）
- 仅 MoneyWiz 桌面版（移动端云数据未测试）
- 与 MoneyWiz 公司无关，**这不是官方产品**
- 没有自动测试，欢迎贡献

## License

MIT — 详见 [LICENSE](./LICENSE)。

## 致谢

感谢 MoneyWiz 团队开发了这款离线优先、未加密 SQLite 的记账工具——这种开放性让本工具成为可能。

如果你觉得这个项目有用，欢迎 Star 或者 Fork 出你自己的扩展版。
