# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Alipay2Moneywiz is a Node.js CLI tool that converts Alipay and WeChat transaction statements into MoneyWiz-compatible CSV format with Chinese field names. The tool handles GBK encoding, CSV/XLSX parsing, account mapping, and date localization.

## Development Environment

**Node.js Version**: ≥14.16.1 (`lts/fermium`)
```bash
nvm install lts/fermium
nvm use lts/fermium
npm install
```

## Commands

### Conversion Scripts
```bash
npm run alipay    # Convert Alipay statement (CSV with GBK encoding)
npm run wechat    # Convert WeChat statement (CSV or XLSX format)
```

Both commands prompt for the source file path interactively. Generated files are saved in the same directory as the source with prefix `【生成】`.

### Direct Execution (for development)
```bash
node alipay.js
node wechat.js
```

## Architecture

### Core Files
- **alipay.js**: Alipay converter - handles GBK → UTF-8 conversion, parses CSV with special header detection (lines starting with `--`), maps accounts, and generates MoneyWiz CSV
- **wechat.js**: WeChat converter - supports both CSV and XLSX formats, handles both file types with unified processing logic
- **account_map.json**: Account name mapping configuration (key = substring in original statement, value = MoneyWiz account name)

### Data Flow
1. Read source file (GBK CSV for Alipay, CSV/XLSX for WeChat)
2. Encoding conversion & header detection (remove metadata lines)
3. Parse into structured records
4. Transform records:
   - Map accounts via `account_map.json`
   - Parse dates to Chinese locale format
   - Handle income/expense/transfer transaction types
   - Apply sign convention (expenses are negative)
5. Generate MoneyWiz CSV with columns: `账户, 转账, 描述, 交易对方, 分类, 日期, 备注, 标签, 金额`

### Key Processing Logic

**Alipay specifics** (alipay.js):
- Skips transactions with status `交易关闭`
- Header detection: finds line starting with `--` and containing `支付宝`
- Removes trailing commas from CSV lines
- Default account: `支付宝余额` when payment method is empty
- Special handling: repayment transactions (还款) are negative amounts

**WeChat specifics** (wechat.js):
- Supports both `.csv` and `.xlsx` input formats
- For XLSX: finds header row containing `交易时间`, then parses data rows
- For CSV: skips lines starting with `--`, processes data after
- Default account: `微信零钱` when payment method is empty or `/`
- Handles both regular transactions and transfers

**Shared patterns**:
- `mapAccount()`: matches substrings from `account_map.json` to map raw account names
- `parseDate()`: converts to Chinese locale format with 24-hour time
- Transaction type logic: distinguishes income/expense/transfer based on `收/支` field

## Configuration

### account_map.json
Map partial account names from source statements to your MoneyWiz account names:
```json
{
  "1234": "农行卡",
  "余额": "支付宝余额",
  "零钱": "微信零钱"
}
```
Update this file before running conversions. If it contains sensitive data, keep a personal copy outside version control.

## Code Style

- ES modules (`import`/`export` syntax, `"type": "module"` in package.json)
- Two-space indentation
- Prefer `const` over `let`
- Lower camelCase for functions (`mainProcess`, `parseDate`)
- Uppercase for constants (`ACCOUNT_MAP`)
- Single quotes for strings, template literals when needed
- Keep side effects in `main()` functions

## Testing & Validation

No automated tests exist. Validate changes by:
1. Running the relevant npm script against sample statements
2. Checking CSV output headers: `账户, 转账, 描述, 交易对方, 分类, 日期, 备注, 标签, 金额`
3. Spot-checking rows for correct date format, sign convention, and account mapping

## Commit Guidelines

Follow Conventional Commits format:
- `feat(alipay): add support for new transaction type`
- `fix(wechat): handle xlsx files with empty rows`
- `refactor: extract date parsing logic`

Recent commit patterns:
- `feat(wechat): add xlsx support for wechat`
- `fix: alipay .csv format changed`
- `skip alipays closed transactions`
