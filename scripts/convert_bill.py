#!/usr/bin/env python3
"""
Main entry point for bill parsing.
Automatically detects bill type and outputs JSON for AI processing.

Usage:
    python convert_bill.py <input_file>
    python convert_bill.py <input_file> --output <output.json>

Output is JSON format that AI will use for:
1. Review transactions with low confidence
2. Query MoneyWiz database for prediction
3. Generate final CSV after user confirmation
"""

import json
import sys
from pathlib import Path

# Add scripts directory to path
SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

from parse_alipay import parse_alipay_csv, load_config
from parse_wechat import parse_wechat


def detect_bill_type(file_path: str) -> str:
    """Detect bill type based on filename and content."""
    file_path = Path(file_path)
    filename = file_path.name.lower()

    # Check filename first
    if "alipay" in filename or "支付宝" in filename:
        return "alipay"
    if "wechat" in filename or "微信" in filename:
        return "wechat"

    # Check content
    ext = file_path.suffix.lower()
    if ext == ".csv":
        try:
            with open(file_path, "r", encoding="gbk", errors="ignore") as f:
                content = f.read(2000)
                if "支付宝" in content:
                    return "alipay"

            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read(2000)
                if "微信支付" in content or "WeChat" in content:
                    return "wechat"
                if "支付宝" in content:
                    return "alipay"
        except Exception:
            pass
    elif ext == ".xlsx":
        return "wechat"

    return "unknown"


def parse_bill(file_path: str) -> dict:
    """Parse bill file and return JSON structure."""
    file_path = Path(file_path)

    if not file_path.exists():
        return {"success": False, "error": f"File not found: {file_path}"}

    bill_type = detect_bill_type(str(file_path))

    if bill_type == "unknown":
        return {
            "success": False,
            "error": f"Cannot detect bill type. Filename should contain '支付宝'/'alipay' or '微信'/'wechat'"
        }

    try:
        config = load_config()

        if bill_type == "alipay":
            result = parse_alipay_csv(str(file_path), config)
        else:
            result = parse_wechat(str(file_path), config)

        result["success"] = True
        return result

    except Exception as e:
        return {
            "success": False,
            "source": bill_type,
            "file": file_path.name,
            "error": str(e)
        }


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    input_file = sys.argv[1]

    # Check for --output flag
    output_file = None
    if "--output" in sys.argv:
        idx = sys.argv.index("--output")
        if idx + 1 < len(sys.argv):
            output_file = sys.argv[idx + 1]

    result = parse_bill(input_file)

    if output_file:
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"Output: {output_file}", file=sys.stderr)
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))

    if result.get("success"):
        stats = result.get("stats", {})
        print(f"\n[{result['source']}] Parsed {stats.get('total', 0)} transactions, "
              f"{stats.get('needs_review', 0)} need review", file=sys.stderr)
        sys.exit(0)
    else:
        print(f"\nError: {result.get('error')}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
