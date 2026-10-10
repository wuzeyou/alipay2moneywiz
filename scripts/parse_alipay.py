#!/usr/bin/env python3
"""
Parse Alipay bill CSV (GBK encoded) to intermediate JSON format.
AI will use this JSON for further processing (category prediction, etc).

Output JSON format:
{
    "source": "alipay",
    "file": "original_filename.csv",
    "parsed_at": "2024-11-22T10:30:00",
    "transactions": [
        {
            "date": "2024/11/15 12:30:45",
            "description": "商品说明",
            "payee_original": "原始交易对方",
            "payee": "简化后交易对方",
            "category": "预测分类或空",
            "category_confidence": "high|medium|low|none",
            "account": "映射后账户",
            "transfer_account": "转账目标账户或空",
            "amount": "-123.45",
            "original_category": "原始分类",
            "payment_method": "原始支付方式",
            "needs_review": true/false
        }
    ],
    "stats": {
        "total": 100,
        "high_confidence": 80,
        "needs_review": 20
    }
}
"""

import csv
import json
import sys
from datetime import datetime
from pathlib import Path

SKILL_DIR = Path(__file__).parent.parent
CONFIG_DIR = SKILL_DIR / "config"

sys.path.insert(0, str(Path(__file__).parent))
from fx import apply_fx  # noqa: E402


def load_config():
    """Load all config files."""
    config = {}

    for name in ["account_map", "category_rules", "payee_rules", "description_rules"]:
        config_file = CONFIG_DIR / f"{name}.json"
        if config_file.exists():
            with open(config_file, "r", encoding="utf-8") as f:
                config[name] = json.load(f)
        else:
            config[name] = {}

    return config


def map_account(payment_method: str, config: dict) -> str:
    """Map payment method to MoneyWiz account name."""
    if not payment_method or not payment_method.strip():
        return config.get("account_map", {}).get("defaults", {}).get("alipay", "支付宝余额")

    account_map = config.get("account_map", {})

    for card_suffix, account_name in account_map.get("bank_cards", {}).items():
        if card_suffix in payment_method:
            return account_name

    for keyword, account_name in account_map.get("keywords", {}).items():
        if keyword in payment_method:
            return account_name

    return account_map.get("defaults", {}).get("alipay", "支付宝余额")


def _resolve_category(value, is_expense: bool):
    """Resolve a rule value to a category name, or None if it does not apply.

    A plain string value applies in both directions. A dict value of the form
    {"expense": ..., "income": ...} picks by the transaction's direction, so a
    payee that both pays out and takes in (闲鱼: resale income vs. platform
    fees) never stamps an income category onto an expense. Returning None lets
    the caller fall through to the next rule.
    """
    if isinstance(value, dict):
        return value.get("expense" if is_expense else "income")
    return value


def predict_category(payee: str, description: str, original_category: str,
                     config: dict, is_expense: bool = True) -> tuple:
    """Predict category. Returns (category, confidence).

    Priority: description keywords > payee. The bill's own category is never
    used as a prediction (see below).
    This ensures specific items like coffee are correctly categorized
    even when ordered through platforms like Meituan.
    """
    rules = config.get("category_rules", {})

    # 1. Keyword match in description (highest priority)
    for keyword, value in rules.get("keyword_category", {}).items():
        if keyword in description:
            category = _resolve_category(value, is_expense)
            if category:
                return category, "high"

    # 2. Exact match payee
    for payee_key, value in rules.get("payee_category", {}).items():
        if payee_key in payee:
            category = _resolve_category(value, is_expense)
            if category:
                return category, "high"

    # 3. Keyword match in payee (fallback)
    for keyword, value in rules.get("keyword_category", {}).items():
        if keyword in payee:
            category = _resolve_category(value, is_expense)
            if category:
                return category, "medium"

    # No rule matched. Never fall back to original_category: Alipay's own
    # category names (数码电器, 日用百货, 收入, ...) do not exist in MoneyWiz's
    # category tree, so returning one yields a plausible-looking but invalid
    # category that misleads the AI review step. The raw value stays available
    # in the JSON output's original_category field for reference.
    return "", "none"


def simplify_payee(payee: str, config: dict) -> str:
    """Simplify payee name."""
    if not payee:
        return ""

    rules = config.get("payee_rules", {})

    for original, simplified in rules.get("simplify", {}).items():
        if original in payee:
            return simplified

    # Auto simplify
    result = payee
    for suffix in ["（上海）", "(上海)", "有限公司", "有限责任公司", "科技", "网络", "信息", "服务"]:
        result = result.replace(suffix, "")

    return result.strip() or payee


def apply_payee_description(payee: str, description: str, config: dict) -> str:
    """Override description based on payee name if rule exists."""
    if not payee:
        return description

    rules = config.get("description_rules", {})
    payee_desc = rules.get("payee_description", {})

    for payee_key, new_desc in payee_desc.items():
        if payee_key.startswith("_"):
            continue
        if payee_key in payee:
            return new_desc

    return description


def simplify_description(description: str, config: dict) -> str:
    """Simplify transaction description by removing noise."""
    import re

    if not description:
        return ""

    rules = config.get("description_rules", {})
    result = description

    # Check if should keep prefix (e.g. "退款-")
    keep_prefixes = rules.get("keep_prefixes", [])
    prefix_to_restore = ""
    for prefix in keep_prefixes:
        if result.startswith(prefix):
            prefix_to_restore = prefix
            result = result[len(prefix):]
            break

    # Apply regex removal patterns
    for pattern in rules.get("remove_patterns", []):
        try:
            result = re.sub(pattern, "", result)
        except re.error:
            pass

    # Apply replacements
    for old, new in rules.get("replacements", {}).items():
        try:
            result = re.sub(old, new, result)
        except re.error:
            if old in result:
                result = result.replace(old, new)

    # Remove suffixes like "等多件"
    for suffix_pattern in rules.get("simplify_suffixes", []):
        try:
            result = re.sub(suffix_pattern, "", result)
        except re.error:
            pass

    # Restore prefix
    if prefix_to_restore:
        result = prefix_to_restore + result

    return result.strip() or description


def format_date(date_str: str) -> str:
    """Format date to YYYY/MM/DD HH:mm:ss."""
    for fmt in ["%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M"]:
        try:
            dt = datetime.strptime(date_str.strip(), fmt)
            return dt.strftime("%Y/%m/%d %H:%M:%S")
        except ValueError:
            continue
    return date_str


def parse_alipay_csv(file_path: str, config: dict) -> dict:
    """Parse Alipay CSV and return structured data."""

    with open(file_path, "r", encoding="gbk", errors="ignore") as f:
        content = f.read()

    lines = content.split("\n")

    # Find data section
    data_start = -1
    for i, line in enumerate(lines):
        if line.startswith("--") and "支付宝" in line:
            data_start = i + 1
            break
        if "交易时间" in line and "金额" in line:
            data_start = i
            break

    if data_start == -1:
        raise ValueError("Cannot find data section in Alipay CSV")

    # Extract data
    data_lines = []
    for line in lines[data_start:]:
        if line.strip() and not line.startswith("--"):
            if line.endswith(","):
                line = line[:-1]
            data_lines.append(line)

    reader = csv.DictReader("\n".join(data_lines).split("\n"))

    transactions = []
    for row in reader:
        if row.get("交易状态", "").strip() == "交易关闭":
            continue

        date = format_date(row.get("交易时间", ""))
        description_original = row.get("商品说明", "").strip()
        payee_original = row.get("交易对方", "").strip()
        income_expense = row.get("收/支", "").strip()
        amount_str = row.get("金额", "0").strip()
        payment_method = row.get("收/付款方式", "").strip()
        original_category = row.get("交易分类", "").strip()

        try:
            amount = float(amount_str.replace(",", ""))
        except ValueError:
            amount = 0.0

        payee = simplify_payee(payee_original, config)
        description = simplify_description(description_original, config)
        # Apply payee-based description override
        description = apply_payee_description(payee, description, config)
        account = map_account(payment_method, config)

        tx = {
            "date": date,
            "description_original": description_original,
            "description": description,
            "payee_original": payee_original,
            "payee": payee,
            "account": account,
            "transfer_account": "",
            "original_category": original_category,
            "payment_method": payment_method,
        }

        # Detect refund: original_category == "退款" or description starts with "退款-"
        is_refund = (
            original_category == "退款"
            or (description_original.startswith("退款-") and income_expense == "不计收支")
        )

        if is_refund:
            # Refund: imported as Income (Z_ENT=38), later converted to Refund (Z_ENT=44)
            # by convert_refunds.py. Description format: "<original> 的退款"
            base_desc = description_original[3:] if description_original.startswith("退款-") else description_original
            base_desc = simplify_description(base_desc, config)
            base_desc = apply_payee_description(payee, base_desc, config)
            tx["description"] = f"{base_desc} 的退款"
            # CSV placeholder: refunds import as Income, and the importer
            # resolves category names among income categories only — an
            # expense category name gets cloned into the income tree. The
            # real category is pinned via category_pk and re-pointed by
            # convert_refunds.py after import.
            # Default is_expense=True: this predicts the ORIGINAL PURCHASE's category,
            # not the incoming refund's.
            _, confidence = predict_category(payee_original, base_desc, "", config)
            tx["category"] = "退款收入"
            tx["category_confidence"] = confidence
            tx["amount"] = str(abs(amount))
            tx["is_refund"] = True
        elif income_expense in ["收入", "支出"]:
            category, confidence = predict_category(payee_original, description, original_category,
                                                    config, is_expense=income_expense == "支出")
            tx["category"] = category
            tx["category_confidence"] = confidence
            tx["amount"] = str(-abs(amount)) if income_expense == "支出" else str(amount)
        else:
            # Transfer (e.g., credit card repayment)
            tx["transfer_account"] = map_account(payee_original, config)
            tx["category"] = ""
            tx["category_confidence"] = "high"
            tx["amount"] = str(-abs(amount)) if "还款" in description else str(amount)

        # Bills report CNY; non-CNY accounts must be converted before the CSV,
        # since the importer reads every amount as the account's own currency.
        apply_fx(tx, config)
        tx["needs_review"] = tx["category_confidence"] in ["low", "none"]
        transactions.append(tx)

    # Stats
    high = sum(1 for tx in transactions if tx["category_confidence"] == "high")
    medium = sum(1 for tx in transactions if tx["category_confidence"] == "medium")
    needs_review = sum(1 for tx in transactions if tx["needs_review"])

    return {
        "source": "alipay",
        "file": Path(file_path).name,
        "parsed_at": datetime.now().isoformat(),
        "transactions": transactions,
        "stats": {
            "total": len(transactions),
            "high_confidence": high,
            "medium_confidence": medium,
            "needs_review": needs_review
        }
    }


def main():
    if len(sys.argv) < 2:
        print("Usage: python parse_alipay.py <input_csv> [output_json]")
        print("\nParse Alipay bill to JSON for AI processing.")
        sys.exit(1)

    input_file = sys.argv[1]
    output_file = sys.argv[2] if len(sys.argv) >= 3 else None

    config = load_config()
    result = parse_alipay_csv(input_file, config)

    if output_file:
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"Output: {output_file}")
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))

    # Print summary to stderr
    stats = result["stats"]
    print(f"\n[Parsed {stats['total']} transactions, {stats['needs_review']} need review]", file=sys.stderr)


if __name__ == "__main__":
    main()
