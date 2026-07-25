#!/usr/bin/env python3
"""
Convert imported Income transactions (Z_ENT=38) into Refund (Z_ENT=44).

After CSV import, MoneyWiz creates refund entries as Income because its CSV
importer does not expose the refund type. This script reads is_refund=true
entries from confirmed.json, locates the matching imported transactions in
MoneyWiz SQLite database, and updates Z_ENT in place. Entries that also pin
original_tx_pk get a ZWITHDRAWREFUNDTRANSACTIONLINK row inserted, linking the
refund to its original withdraw transaction (model feature since 2026-07).

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

# Z_ENT values for the 2026-07 Core Data model. These shift whenever a MoneyWiz
# update migrates the model; verify after updates with:
#   SELECT Z_ENT, Z_NAME FROM Z_PRIMARYKEY ORDER BY Z_ENT;
ENT_ACCOUNTS = (10, 11, 12, 13, 14, 15, 16, 17)
ENT_CATEGORY = 20
ENT_DEPOSIT = 38
ENT_REFUND = 44
ENT_WITHDRAW = 48
ENT_REFUND_LINK = 51


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
        f"SELECT Z_PK FROM ZSYNCOBJECT "
        f"WHERE Z_ENT IN {ENT_ACCOUNTS} AND ZNAME = ?",
        (account_name,),
    )
    row = cur.fetchone()
    return row[0] if row else None


def apply_refund_categories(conn: sqlite3.Connection, converted: list, dry_run: bool) -> list:
    """Point each converted refund at the exact category pinned in confirmed.json.

    The only trusted source is category_pk, resolved during the confirm step
    against the original consumption — the one place with enough context to know
    which category object the refund belongs to. Two tempting shortcuts are
    deliberately rejected: a category NAME cannot disambiguate (this tree has
    many same-name categories), and matching a prior expense by amount+date
    silently mis-assigns (a same-amount expense is usually a different purchase —
    e.g. an 8.3 transit top-up refund matching an 8.3 coffee). When no valid
    category_pk is pinned, the imported assignment is left untouched and reported
    so it can be pinned or fixed by hand. The script never guesses.

    converted: list of (tx_dict, refund_pk)
    Returns list of {desc, pk, target, source}; source in {pinned, unresolved}.
    """
    results = []
    for tx, pk in converted:
        target, source = None, "unresolved"

        cat_pk = tx.get("category_pk")
        if cat_pk is not None:
            valid = conn.execute(
                f"SELECT 1 FROM ZSYNCOBJECT WHERE Z_PK = ? AND Z_ENT = {ENT_CATEGORY}",
                (cat_pk,),
            ).fetchone()
            if valid:
                target, source = int(cat_pk), "pinned"

        old_cats = []
        if target is not None:
            assigns = conn.execute(
                "SELECT Z_PK, ZCATEGORY FROM ZCATEGORYASSIGMENT WHERE ZTRANSACTION = ?",
                (pk,),
            ).fetchall()
            old_cats = sorted({c for _, c in assigns if c is not None and c != target})
            if not dry_run:
                for assign_pk, _ in assigns:
                    conn.execute(
                        "UPDATE ZCATEGORYASSIGMENT SET ZCATEGORY = ? WHERE Z_PK = ?",
                        (target, assign_pk),
                    )

        results.append(
            {
                "desc": tx.get("description", ""),
                "pk": pk,
                "target": target,
                "source": source,
                "old_cats": old_cats,
            }
        )
    return results


def drop_importer_clones(conn: sqlite3.Connection, cat_results: list, batch_pks: list, dry_run: bool) -> list:
    """Drop income-tree category clones the CSV importer created for refunds.

    Refunds import as Income, so the importer resolves their CSV category
    among income categories only — an expense category name silently becomes
    a brand-new top-level income clone. After assignments are re-pinned to
    category_pk, such a clone is deletable, but only under strict proof it
    IS one: same ZNAME2 as the pinned original, income side (ZTYPE2=2), no
    assignment references outside this batch, no subcategories. Legitimate
    standing categories (e.g. the 退款收入 CSV placeholder) never match.

    Returns list of (category_name, clone_pk, original_pk).
    """
    candidates = {}
    for r in cat_results:
        if r["source"] != "pinned":
            continue
        for old in r["old_cats"]:
            candidates.setdefault(old, r["target"])

    dropped = []
    placeholders = ",".join("?" * len(batch_pks)) or "NULL"
    for clone_pk, target_pk in candidates.items():
        clone = conn.execute(
            f"SELECT ZNAME2, ZTYPE2 FROM ZSYNCOBJECT WHERE Z_PK = ? AND Z_ENT = {ENT_CATEGORY}",
            (clone_pk,),
        ).fetchone()
        orig = conn.execute(
            f"SELECT ZNAME2 FROM ZSYNCOBJECT WHERE Z_PK = ? AND Z_ENT = {ENT_CATEGORY}",
            (target_pk,),
        ).fetchone()
        if not clone or not orig or clone[0] != orig[0] or clone[1] != 2:
            continue
        external_refs = conn.execute(
            f"SELECT COUNT(*) FROM ZCATEGORYASSIGMENT "
            f"WHERE ZCATEGORY = ? AND ZTRANSACTION IS NOT NULL "
            f"AND ZTRANSACTION NOT IN ({placeholders})",
            [clone_pk] + batch_pks,
        ).fetchone()[0]
        children = conn.execute(
            "SELECT COUNT(*) FROM ZSYNCOBJECT WHERE ZPARENTCATEGORY = ?",
            (clone_pk,),
        ).fetchone()[0]
        if external_refs or children:
            continue
        if not dry_run:
            conn.execute(
                "DELETE FROM ZCATEGORYASSIGMENT WHERE ZCATEGORY = ? AND ZTRANSACTION IS NULL",
                (clone_pk,),
            )
            conn.execute(
                f"DELETE FROM ZSYNCOBJECT WHERE Z_PK = ? AND Z_ENT = {ENT_CATEGORY}",
                (clone_pk,),
            )
        dropped.append((clone[0], clone_pk, target_pk))
    return dropped


def link_refunds_to_originals(conn: sqlite3.Connection, converted: list, dry_run: bool) -> list:
    """Insert ZWITHDRAWREFUNDTRANSACTIONLINK rows tying refunds to originals.

    Since the 2026-07 model, MoneyWiz stores refund -> original-withdraw links
    in a dedicated join table. Same trust rule as categories: only an explicit
    original_tx_pk pinned in confirmed.json is accepted, and it must point at
    an existing Withdraw row. No pin, an invalid pin, or an already-linked
    refund is reported and left alone — the script never guesses the original.

    converted: list of (tx_dict, refund_pk)
    Returns list of {desc, refund_pk, original_pk, status};
    status in {linked, not_pinned, invalid, exists}.
    """
    results = []
    for tx, refund_pk in converted:
        orig_pk = tx.get("original_tx_pk")
        status = "not_pinned"

        if orig_pk is not None:
            valid = conn.execute(
                f"SELECT 1 FROM ZSYNCOBJECT WHERE Z_PK = ? AND Z_ENT = {ENT_WITHDRAW}",
                (orig_pk,),
            ).fetchone()
            if not valid:
                status = "invalid"
            else:
                already = conn.execute(
                    "SELECT 1 FROM ZWITHDRAWREFUNDTRANSACTIONLINK "
                    "WHERE ZREFUNDTRANSACTION = ?",
                    (refund_pk,),
                ).fetchone()
                if already:
                    status = "exists"
                else:
                    status = "linked"
                    if not dry_run:
                        row = conn.execute(
                            "SELECT Z_MAX FROM Z_PRIMARYKEY WHERE Z_ENT = ?",
                            (ENT_REFUND_LINK,),
                        ).fetchone()
                        new_pk = (row[0] if row and row[0] else 0) + 1
                        conn.execute(
                            "UPDATE Z_PRIMARYKEY SET Z_MAX = ? WHERE Z_ENT = ?",
                            (new_pk, ENT_REFUND_LINK),
                        )
                        conn.execute(
                            "INSERT INTO ZWITHDRAWREFUNDTRANSACTIONLINK "
                            "(Z_PK, Z_ENT, Z_OPT, ZREFUNDTRANSACTION, ZWITHDRAWTRANSACTION) "
                            "VALUES (?, ?, 1, ?, ?)",
                            (new_pk, ENT_REFUND_LINK, refund_pk, int(orig_pk)),
                        )

        results.append(
            {
                "desc": tx.get("description", ""),
                "refund_pk": refund_pk,
                "original_pk": orig_pk,
                "status": status,
            }
        )
    return results


def find_refund_match(
    conn: sqlite3.Connection,
    account_pk: int,
    amount: float,
    description: str,
    target_cd: float,
) -> tuple:
    cur = conn.execute(
        f"""
        SELECT Z_PK FROM ZSYNCOBJECT
        WHERE Z_ENT = {ENT_DEPOSIT}
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
                        f"UPDATE ZSYNCOBJECT SET Z_ENT = {ENT_REFUND} "
                        f"WHERE Z_PK = ? AND Z_ENT = {ENT_DEPOSIT}",
                        (pk,),
                    )
                success.append((tx, pk))

        cat_results = apply_refund_categories(conn, success, dry_run)
        link_results = link_refunds_to_originals(conn, success, dry_run)
        clone_drops = drop_importer_clones(
            conn, cat_results, [pk for _, pk in success], dry_run
        )

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

    if clone_drops:
        verb = "would drop" if dry_run else "dropped"
        print(f"\nImporter-created category clones {verb}: {len(clone_drops)}")
        for name, clone_pk, orig_pk in clone_drops:
            print(f"  ✓ {name}: clone Z_PK={clone_pk} -> original Z_PK={orig_pk}")

    if cat_results:
        pinned = [r for r in cat_results if r["source"] == "pinned"]
        unresolved = [r for r in cat_results if r["source"] == "unresolved"]
        verb = "would set" if dry_run else "set"
        print(
            f"\nRefund categories {verb}: "
            f"{len(pinned)} pinned, {len(unresolved)} unresolved"
        )
        for r in unresolved:
            print(
                f"  ! {r['desc'][:30]:30s} unresolved — left as imported; "
                f"pin category_pk in JSON or fix in MoneyWiz"
            )

    if link_results:
        linked = [r for r in link_results if r["status"] == "linked"]
        skipped = [r for r in link_results if r["status"] in ("invalid", "exists")]
        not_pinned = [r for r in link_results if r["status"] == "not_pinned"]
        verb = "would link" if dry_run else "linked"
        print(
            f"\nOriginal-transaction links: {len(linked)} {verb}, "
            f"{len(skipped)} skipped, {len(not_pinned)} not pinned"
        )
        for r in linked:
            print(f"  ✓ {r['desc'][:30]:30s} refund Z_PK={r['refund_pk']} -> withdraw Z_PK={r['original_pk']}")
        for r in skipped:
            print(f"  ! {r['desc'][:30]:30s} {r['status']} — left alone")

    if failures:
        print(f"\nFailures: {len(failures)}")
        for tx, reason in failures:
            print(
                f"  ✗ {tx['date'][:16]} {tx['description'][:30]:30s} "
                f"{tx['amount']:>8s}  ({reason})"
            )
        print(
            f"\nFailed entries remain as Income (Z_ENT={ENT_DEPOSIT}). "
            "Convert them manually in MoneyWiz if needed."
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
