#!/usr/bin/env python3
"""
Generate MoneyWiz-compatible CSV from confirmed transaction JSON.

Usage:
    python generate_csv.py <input.json> [output_dir]
    cat confirmed.json | python generate_csv.py - [output_dir]

Input: JSON with confirmed transactions (same format as convert_bill.py output).
       Accepts a list of transactions or a dict with 'transactions' key.
Output: CSV file in MoneyWiz import format.

Default output directory: ./bills/processed/
Filename auto-generated: bills_YYYYMMDD-YYYYMMDD.csv
"""

import csv
import json
import sys
from datetime import datetime
from pathlib import Path


# MoneyWiz CSV columns and their JSON field mappings
CSV_COLUMNS = [
    ('账户', 'account'),
    ('转账', 'transfer_account'),
    ('描述', 'description'),
    ('交易对方', 'payee'),
    ('分类', 'category'),
    ('日期', 'date'),
    ('备注', 'notes'),
    ('标签', 'tags'),
    ('金额', 'amount'),
]


def load_transactions(input_path: str) -> list:
    """Load transactions from JSON file or stdin."""
    if input_path == '-':
        data = json.load(sys.stdin)
    else:
        with open(input_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

    if isinstance(data, list):
        return data
    if isinstance(data, dict) and 'transactions' in data:
        return data['transactions']
    raise ValueError("JSON must be a list or contain 'transactions' key")


def generate_filename(transactions: list) -> str:
    """Generate filename from date range: bills_YYYYMMDD-YYYYMMDD.csv"""
    dates = []
    for t in transactions:
        try:
            dt = datetime.strptime(t['date'][:10], '%Y/%m/%d')
            dates.append(dt)
        except (ValueError, KeyError):
            continue

    if not dates:
        return f"bills_{datetime.now().strftime('%Y%m%d')}.csv"

    min_date = min(dates).strftime('%Y%m%d')
    max_date = max(dates).strftime('%Y%m%d')
    return f"bills_{min_date}-{max_date}.csv"


def generate_csv(transactions: list, output_path: str) -> str:
    """Generate MoneyWiz CSV from transaction list."""
    # Sort by date descending
    transactions.sort(key=lambda t: t.get('date', ''), reverse=True)

    with open(output_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow([col for col, _ in CSV_COLUMNS])

        for t in transactions:
            row = [t.get(field, '') or '' for _, field in CSV_COLUMNS]
            writer.writerow(row)

    return output_path


def main():
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        sys.exit(1)

    input_path = sys.argv[1]
    output_dir = sys.argv[2] if len(sys.argv) > 2 else './bills/processed'

    Path(output_dir).mkdir(parents=True, exist_ok=True)

    transactions = load_transactions(input_path)
    if not transactions:
        print("Error: No transactions found", file=sys.stderr)
        sys.exit(1)

    filename = generate_filename(transactions)
    output_path = str(Path(output_dir) / filename)

    generate_csv(transactions, output_path)

    expenses = sum(float(t['amount']) for t in transactions if float(t['amount']) < 0)
    income = sum(float(t['amount']) for t in transactions if float(t['amount']) > 0)

    print(f"Generated: {output_path}")
    print(f"Transactions: {len(transactions)}")
    print(f"Expenses: ¥{abs(expenses):.2f}, Income: ¥{income:.2f}")


if __name__ == '__main__':
    main()
