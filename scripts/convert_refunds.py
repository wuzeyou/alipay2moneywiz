#!/usr/bin/env python3
"""
Convert imported Income transactions (Z_ENT=37) into Refund (Z_ENT=43).

After CSV import, MoneyWiz creates refund entries as Income because its CSV
importer does not expose Z_ENT=43. This script reads is_refund=true entries
from confirmed.json, locates the matching imported transactions in MoneyWiz
SQLite database, and updates Z_ENT in place.

Safety:
  - Quits MoneyWiz before writing (avoids in-flight DB corruption / sync drift)
  - Backs up the DB to ./bills/backups/ before any UPDATE
  - Each match must satisfy: account + amount + description + date window;
    only single-row matches are committed (>1 hits are skipped, not guessed)
  - Single transaction wraps all UPDATEs; rollback on any unexpected error

Usage:
    python3 convert_refunds.py <confirmed.json> [--dry-run]

    --dry-run: report matches without modifying the database (no quit, no backup)
"""
import json
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

DB_PATH = (
    Path.home()
    / "Library/Containers/com.moneywiz.personalfinance"
    / "Data/Documents/.AppData/ipadMoneyWiz.sqlite"
)
CORE_DATA_EPOCH = 978307200  # seconds between Unix epoch (1970) and Core Data epoch (2001)
TIME_WINDOW_SEC = 300  # ±5min around the recorded date
AMOUNT_TOLERANCE = 0.001
QUIT_WAIT_SEC = 30


def is_moneywiz_running() -> bool:
    result = subprocess.run(
        ["pgrep", "-x", "MoneyWiz"], capture_output=True, text=True
    )
    return result.returncode == 0


def quit_moneywiz() -> bool:
    subprocess.run(
        ["osascript", "-e", 'tell application "MoneyWiz" to quit'],
        capture_output=True,
    )
    for _ in range(QUIT_WAIT_SEC):
        if not is_moneywiz_running():
            return True
        time.sleep(1)
    return False


def backup_db(db_path: Path) -> Path:
    backup_dir = Path.cwd() / "bills" / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = backup_dir / f"ipadMoneyWiz.backup-{ts}.sqlite"
    shutil.copy2(db_path, backup)
    return backup


def parse_date_to_coredata(date_str: str) -> float:
    dt = datetime.strptime(date_str, "%Y/%m/%d %H:%M:%S")
    return dt.timestamp() - CORE_DATA_EPOCH


def find_account_pk(conn: sqlite3.Connection, account_name: str):
    cur = conn.execute(
        "SELECT Z_PK FROM ZSYNCOBJECT "
        "WHERE Z_ENT IN (9,10,11,12,13,14,15,16) AND ZNAME = ?",
        (account_name,),
    )
    row = cur.fetchone()
    return row[0] if row else None


def find_refund_match(
    conn: sqlite3.Connection,
    account_pk: int,
    amount: float,
    description: str,
    target_cd: float,
) -> tuple:
    cur = conn.execute(
        """
        SELECT Z_PK FROM ZSYNCOBJECT
        WHERE Z_ENT = 37
          AND ZACCOUNT2 = ?
          AND ABS(ZAMOUNT1 - ?) < ?
          AND ZDESC2 = ?
          AND ABS(ZDATE1 - ?) < ?
          AND ZSENDERACCOUNT IS NULL
          AND ZRECIPIENTACCOUNT IS NULL
        """,
        (
            account_pk,
            amount,
            AMOUNT_TOLERANCE,
            description,
            target_cd,
            TIME_WINDOW_SEC,
        ),
    )
    return cur.fetchall()


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry_run = "--dry-run" in sys.argv

    if not args:
        print("Usage: convert_refunds.py <confirmed.json> [--dry-run]", file=sys.stderr)
        sys.exit(1)

    json_path = Path(args[0])
    if not json_path.exists():
        print(f"ERROR: {json_path} not found", file=sys.stderr)
        sys.exit(1)

    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)

    txs = data["transactions"] if isinstance(data, dict) else data
    refunds = [t for t in txs if t.get("is_refund")]

    if not refunds:
        print("No refund transactions in JSON, nothing to do.")
        return

    mode = "[DRY-RUN]" if dry_run else ""
    print(f"{mode} Found {len(refunds)} refund(s) to convert.")

    if not dry_run and is_moneywiz_running():
        print("MoneyWiz is running; quitting...")
        if not quit_moneywiz():
            print("ERROR: Failed to quit MoneyWiz within timeout", file=sys.stderr)
            sys.exit(1)
        print("MoneyWiz quit.")

    if not DB_PATH.exists():
        print(f"ERROR: Database not found at {DB_PATH}", file=sys.stderr)
        sys.exit(1)

    if not dry_run:
        backup = backup_db(DB_PATH)
        print(f"Backup: {backup}")

    conn = sqlite3.connect(str(DB_PATH))
    try:
        if not dry_run:
            conn.execute("BEGIN IMMEDIATE")

        success = []
        failures = []

        for tx in refunds:
            account_pk = find_account_pk(conn, tx["account"])
            if not account_pk:
                failures.append((tx, f"account not found: {tx['account']}"))
                continue

            try:
                amount = float(tx["amount"])
                target_cd = parse_date_to_coredata(tx["date"])
            except (ValueError, KeyError) as e:
                failures.append((tx, f"bad data: {e}"))
                continue

            description = tx["description"]
            rows = find_refund_match(conn, account_pk, amount, description, target_cd)

            if len(rows) == 0:
                failures.append((tx, "no match"))
            elif len(rows) > 1:
                pks = ", ".join(str(r[0]) for r in rows)
                failures.append((tx, f"multiple matches ({len(rows)}): Z_PK={pks}"))
            else:
                pk = rows[0][0]
                if not dry_run:
                    conn.execute(
                        "UPDATE ZSYNCOBJECT SET Z_ENT = 43 WHERE Z_PK = ? AND Z_ENT = 37",
                        (pk,),
                    )
                success.append((tx, pk))

        if not dry_run:
            conn.commit()
    except Exception as e:
        if not dry_run:
            conn.rollback()
        print(f"ERROR: rolled back due to {e}", file=sys.stderr)
        raise
    finally:
        conn.close()

    print()
    verb = "would convert" if dry_run else "converted"
    print(f"Result: {len(success)}/{len(refunds)} {verb}")
    for tx, pk in success:
        print(f"  ✓ {tx['date'][:16]} {tx['description'][:30]:30s} {tx['amount']:>8s}  Z_PK={pk}")

    if failures:
        print(f"\nFailures: {len(failures)}")
        for tx, reason in failures:
            print(
                f"  ✗ {tx['date'][:16]} {tx['description'][:30]:30s} "
                f"{tx['amount']:>8s}  ({reason})"
            )
        print(
            "\nFailed entries remain as Income (Z_ENT=37). "
            "Convert them manually in MoneyWiz if needed."
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
