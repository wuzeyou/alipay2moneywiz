#!/usr/bin/env python3
"""
MoneyWiz Database Query Tool
Query transactions, accounts, categories from MoneyWiz SQLite database.
"""

import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path.home() / "Library/Containers/com.moneywiz.personalfinance/Data/Documents/.AppData/ipadMoneyWiz.sqlite"

# Core Data timestamp offset (2001-01-01 to 1970-01-01)
COREDATA_OFFSET = 978307200

# Transaction types: Deposit(37), TransferDeposit(45), TransferWithdraw(46), Withdraw(47)
TRANSACTION_TYPES = (37, 45, 46, 47)


def get_connection():
    """Get database connection."""
    if not DB_PATH.exists():
        print(f"Error: Database not found at {DB_PATH}")
        sys.exit(1)
    return sqlite3.connect(DB_PATH)


def format_amount(amount):
    """Format amount with sign and 2 decimal places."""
    if amount is None:
        return "N/A"
    return f"{amount:,.2f}"


def query_accounts(show_archived=False):
    """List all accounts."""
    conn = get_connection()
    cursor = conn.cursor()

    sql = """
    SELECT Z_PK, ZNAME, ZTYPE, ZCURRENCYNAME, ZBALLANCE, ZOPENINGBALANCE, ZARCHIVED
    FROM ZSYNCOBJECT
    WHERE Z_ENT IN (9,10,11,12,13,14,15,16)
    """
    if not show_archived:
        sql += " AND (ZARCHIVED = 0 OR ZARCHIVED IS NULL)"
    sql += " ORDER BY ZARCHIVED, ZCURRENCYNAME, ZNAME"

    cursor.execute(sql)
    rows = cursor.fetchall()
    conn.close()

    print(f"\n{'ID':<6} {'Account':<20} {'Currency':<8} {'Balance':>12} {'Archived'}")
    print("-" * 60)
    for row in rows:
        archived = "Yes" if row[6] else ""
        print(f"{row[0]:<6} {(row[1] or ''):<20} {(row[3] or ''):<8} {format_amount(row[4]):>12} {archived}")


def query_recent(limit=20):
    """Query recent transactions."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(f"""
    SELECT
        datetime(t.ZDATE1 + {COREDATA_OFFSET}, 'unixepoch', 'localtime') as Date,
        t.ZAMOUNT1 as Amount,
        t.ZDESC2 as Description,
        a.ZNAME as Account,
        p.ZNAME6 as Payee,
        a.ZCURRENCYNAME as Currency,
        t.Z_ENT as Type
    FROM ZSYNCOBJECT t
    LEFT JOIN ZSYNCOBJECT a ON t.ZACCOUNT2 = a.Z_PK
    LEFT JOIN ZSYNCOBJECT p ON t.ZPAYEE2 = p.Z_PK
    WHERE t.Z_ENT IN {TRANSACTION_TYPES} AND t.ZDATE1 IS NOT NULL
    ORDER BY t.ZDATE1 DESC
    LIMIT ?
    """, (limit,))

    rows = cursor.fetchall()
    conn.close()

    type_map = {37: "收入", 45: "转入", 46: "转出", 47: "支出"}
    print(f"\n{'Date':<20} {'Amount':>12} {'Type':<4} {'Account':<12} {'Description'}")
    print("-" * 75)
    for row in rows:
        date = row[0][:16] if row[0] else ""
        amount = format_amount(row[1])
        tx_type = type_map.get(row[6], "")
        account = (row[3] or "")[:12]
        desc = (row[2] or "")[:25]
        print(f"{date:<20} {amount:>12} {tx_type:<4} {account:<12} {desc}")


def query_monthly(months=6):
    """Query monthly income/expense summary."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(f"""
    SELECT
        strftime('%Y-%m', datetime(ZDATE1 + {COREDATA_OFFSET}, 'unixepoch', 'localtime')) as Month,
        SUM(CASE WHEN ZAMOUNT1 > 0 THEN ZAMOUNT1 ELSE 0 END) as Income,
        SUM(CASE WHEN ZAMOUNT1 < 0 THEN ABS(ZAMOUNT1) ELSE 0 END) as Expense,
        SUM(ZAMOUNT1) as Net
    FROM ZSYNCOBJECT
    WHERE Z_ENT IN (37, 47) AND ZDATE1 IS NOT NULL
    GROUP BY Month
    ORDER BY Month DESC
    LIMIT ?
    """, (months,))

    rows = cursor.fetchall()
    conn.close()

    print(f"\n{'Month':<10} {'Income':>14} {'Expense':>14} {'Net':>14}")
    print("-" * 55)
    for row in rows:
        print(f"{row[0]:<10} {format_amount(row[1]):>14} {format_amount(row[2]):>14} {format_amount(row[3]):>14}")


def query_categories(year_month=None):
    """Query expense by category."""
    conn = get_connection()
    cursor = conn.cursor()

    # Default to current month
    if not year_month:
        year_month = datetime.now().strftime('%Y-%m')

    cursor.execute(f"""
    SELECT
        COALESCE(pc.ZNAME2, c.ZNAME2) as ParentCategory,
        c.ZNAME2 as Category,
        SUM(ABS(t.ZAMOUNT1)) as Total,
        COUNT(*) as Count
    FROM ZSYNCOBJECT t
    JOIN ZCATEGORYASSIGMENT ca ON t.Z_PK = ca.ZTRANSACTION
    JOIN ZSYNCOBJECT c ON ca.ZCATEGORY = c.Z_PK
    LEFT JOIN ZSYNCOBJECT pc ON c.ZPARENTCATEGORY = pc.Z_PK
    WHERE t.Z_ENT = 47
      AND t.ZAMOUNT1 < 0
      AND strftime('%Y-%m', datetime(t.ZDATE1 + {COREDATA_OFFSET}, 'unixepoch', 'localtime')) = ?
    GROUP BY c.Z_PK
    ORDER BY Total DESC
    """, (year_month,))

    rows = cursor.fetchall()
    conn.close()

    print(f"\nCategory expenses for {year_month}:")
    print(f"\n{'Parent':<15} {'Category':<15} {'Total':>12} {'Count':>6}")
    print("-" * 52)
    total = 0
    for row in rows:
        parent = (row[0] or "")[:15]
        cat = (row[1] or "")[:15]
        print(f"{parent:<15} {cat:<15} {format_amount(row[2]):>12} {row[3]:>6}")
        total += row[2] or 0
    print("-" * 52)
    print(f"{'Total':<31} {format_amount(total):>12}")


def query_payees(limit=15):
    """Query top payees by spending."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(f"""
    SELECT
        p.ZNAME6 as Payee,
        SUM(ABS(t.ZAMOUNT1)) as Total,
        COUNT(*) as Count,
        AVG(ABS(t.ZAMOUNT1)) as Avg
    FROM ZSYNCOBJECT t
    JOIN ZSYNCOBJECT p ON t.ZPAYEE2 = p.Z_PK
    WHERE t.Z_ENT = 47 AND t.ZAMOUNT1 < 0
    GROUP BY p.Z_PK
    ORDER BY Total DESC
    LIMIT ?
    """, (limit,))

    rows = cursor.fetchall()
    conn.close()

    print(f"\n{'Payee':<25} {'Total':>12} {'Count':>6} {'Avg':>10}")
    print("-" * 56)
    for row in rows:
        payee = (row[0] or "")[:25]
        print(f"{payee:<25} {format_amount(row[1]):>12} {row[2]:>6} {format_amount(row[3]):>10}")


def search_transactions(keyword, limit=30):
    """Search transactions by description."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(f"""
    SELECT
        datetime(t.ZDATE1 + {COREDATA_OFFSET}, 'unixepoch', 'localtime') as Date,
        t.ZAMOUNT1 as Amount,
        t.ZDESC2 as Description,
        a.ZNAME as Account,
        p.ZNAME6 as Payee,
        t.Z_ENT as Type
    FROM ZSYNCOBJECT t
    LEFT JOIN ZSYNCOBJECT a ON t.ZACCOUNT2 = a.Z_PK
    LEFT JOIN ZSYNCOBJECT p ON t.ZPAYEE2 = p.Z_PK
    WHERE t.Z_ENT IN {TRANSACTION_TYPES}
      AND t.ZDATE1 IS NOT NULL
      AND (t.ZDESC2 LIKE ? OR p.ZNAME6 LIKE ?)
    ORDER BY t.ZDATE1 DESC
    LIMIT ?
    """, (f"%{keyword}%", f"%{keyword}%", limit))

    rows = cursor.fetchall()
    conn.close()

    type_map = {37: "收入", 45: "转入", 46: "转出", 47: "支出"}
    print(f"\nSearch results for '{keyword}':")
    print(f"\n{'Date':<20} {'Amount':>12} {'Type':<4} {'Account':<12} {'Description'}")
    print("-" * 70)
    for row in rows:
        date = row[0][:16] if row[0] else ""
        tx_type = type_map.get(row[5], "")
        print(f"{date:<20} {format_amount(row[1]):>12} {tx_type:<4} {(row[3] or ''):<12} {(row[2] or '')[:25]}")

    if not rows:
        print("No transactions found.")


def print_usage():
    """Print usage information."""
    print("""
MoneyWiz Query Tool

Usage:
    python3 moneywiz_query.py <command> [args]

Commands:
    accounts [--all]      List accounts (--all includes archived)
    recent [limit]        Recent transactions (default: 20)
    monthly [months]      Monthly summary (default: 6)
    categories [YYYY-MM]  Expense by category (default: current month)
    payees [limit]        Top payees by spending (default: 15)
    search <keyword>      Search transactions

Examples:
    python3 moneywiz_query.py recent 30
    python3 moneywiz_query.py categories 2024-11
    python3 moneywiz_query.py search keyword
""")


def main():
    if len(sys.argv) < 2:
        print_usage()
        return

    command = sys.argv[1].lower()

    if command == "accounts":
        show_all = "--all" in sys.argv
        query_accounts(show_archived=show_all)
    elif command == "recent":
        limit = int(sys.argv[2]) if len(sys.argv) > 2 else 20
        query_recent(limit)
    elif command == "monthly":
        months = int(sys.argv[2]) if len(sys.argv) > 2 else 6
        query_monthly(months)
    elif command == "categories":
        year_month = sys.argv[2] if len(sys.argv) > 2 else None
        query_categories(year_month)
    elif command == "payees":
        limit = int(sys.argv[2]) if len(sys.argv) > 2 else 15
        query_payees(limit)
    elif command == "search":
        if len(sys.argv) < 3:
            print("Error: Please provide a search keyword")
            return
        search_transactions(sys.argv[2])
    else:
        print(f"Unknown command: {command}")
        print_usage()


if __name__ == "__main__":
    main()
