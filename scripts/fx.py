#!/usr/bin/env python3
"""Foreign-currency handling for non-CNY MoneyWiz accounts.

Alipay/WeChat bills always report amounts in CNY, but MoneyWiz stores every
transaction in its account's own currency, and the CSV importer has no column
for currency or exchange rate -- the 12 mappable fields are Date, Amount,
Amount(Expenses), Amount(Incomes), Description, Payee, Category, Account,
Transfers, Check #, Memo, Tags. Whatever number the CSV carries is read as the
account's currency, so 13.25 CNY imported into an SGD account silently becomes
13.25 SGD (~5x too much).

Conversion therefore has to happen before the CSV is written. The rate is
approximate by design: the real charge is only known once the card statement
arrives. The CNY original is preserved in 'amount_cny' and echoed into the
memo column so the row can be reconciled against that statement later.

For reference, this is how MoneyWiz itself stores a cross-currency
transaction (a WithdrawTransaction on a CNY account paying USD):

    ZAMOUNT1              = -13.426729   <- account currency, drives balance
    ZORIGINALAMOUNT       = -1.99        <- original currency amount
    ZORIGINALCURRENCY     = 'USD'
    ZORIGINALEXCHANGERATE = 6.7471       <- ZAMOUNT1 = ZORIGINALAMOUNT * rate

The CSV route can only supply ZAMOUNT1, which is why the memo carries the rest.
"""

import re

CNY = "CNY"

# Matches a stamp written by build_memo, so re-running with a refreshed rate
# replaces it instead of stacking another copy in front.
_STAMP_RE = re.compile(r"^原价 ¥[\d.]+ @[\d.]+(?: \| )?")


def strip_memo_stamp(notes: str) -> str:
    """Remove a previously written FX stamp from a memo."""
    return _STAMP_RE.sub("", notes, count=1)


def get_account_currency(account: str, config: dict) -> str:
    """Return the account's currency; 'CNY' for ordinary CNY accounts."""
    mapping = config.get("account_map", {}).get("account_currency", {})
    return mapping.get(account, CNY)


def get_fallback_rate(currency: str, config: dict):
    """CNY per 1 unit of `currency`, from config. None when unknown/invalid."""
    rates = config.get("account_map", {}).get("fx_fallback_rates", {})
    try:
        rate = float(rates.get(currency))
    except (TypeError, ValueError):
        return None
    return rate if rate > 0 else None


def build_memo(amount_cny: float, rate: float, existing: str = "") -> str:
    """Memo stamp preserving the CNY original and the rate actually used."""
    stamp = f"原价 ¥{abs(amount_cny):.2f} @{rate:.4f}"
    return f"{stamp} | {existing}" if existing else stamp


def apply_fx(tx: dict, config: dict, rate=None, rate_source="config_fallback") -> bool:
    """Convert tx['amount'] from CNY into the account's currency, in place.

    Returns True when a conversion happened. Sets needs_fx on every non-CNY
    transaction -- including ones already converted -- because the rate is an
    estimate the AI step may refresh and the user should see flagged.
    """
    currency = get_account_currency(tx.get("account", ""), config)
    if currency == CNY:
        return False

    tx["account_currency"] = currency
    tx["needs_fx"] = True

    if rate is None:
        rate = get_fallback_rate(currency, config)
    if rate is None:
        # Unknown rate: leave the amount untouched rather than guess, and flag
        # it so the AI step resolves it instead of a CNY number reaching the
        # CSV as foreign currency.
        tx["fx_rate"] = None
        tx["fx_rate_source"] = "unresolved"
        return False

    source_amount = tx.get("amount_cny", tx.get("amount", 0))
    try:
        amount_cny = float(source_amount)
    except (TypeError, ValueError):
        tx["fx_rate"] = None
        tx["fx_rate_source"] = "unresolved"
        return False

    tx["amount_cny"] = f"{amount_cny:.2f}"
    tx["amount"] = f"{amount_cny / rate:.2f}"
    tx["fx_rate"] = rate
    tx["fx_rate_source"] = rate_source
    tx["notes"] = build_memo(amount_cny, rate, strip_memo_stamp(tx.get("notes", "") or ""))
    return True
