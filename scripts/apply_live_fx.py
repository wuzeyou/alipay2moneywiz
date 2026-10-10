#!/usr/bin/env python3
"""Refresh FX-converted amounts in a parsed-bill JSON using a live rate.

The parsers already convert non-CNY accounts using the fallback rates in
config/account_map.json. This script re-does that conversion with today's
rate, so the CSV lands closer to what the card will actually charge.

Usage:
    python apply_live_fx.py <input.json> [output.json]
    python apply_live_fx.py <input.json> --rate SGD=5.2824 [output.json]
    cat parsed.json | python apply_live_fx.py - > refreshed.json

With no --rate, rates are fetched from open.er-api.com (free, no API key).
When the fetch fails the existing amounts are left alone and the failure is
reported on stderr -- never silently importing a stale or CNY number.

Accepts a list of transactions or a dict with a 'transactions' key, and
writes back in the same shape.
"""

import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import fx

SKILL_DIR = Path(__file__).parent.parent
CONFIG_DIR = SKILL_DIR / "config"
RATE_API = "https://open.er-api.com/v6/latest/CNY"
TIMEOUT = 15


def load_config() -> dict:
    """Load account_map only -- the FX path needs nothing else."""
    config = {}
    config_file = CONFIG_DIR / "account_map.json"
    if config_file.exists():
        with open(config_file, "r", encoding="utf-8") as f:
            config["account_map"] = json.load(f)
    return config


def fetch_live_rates(currencies: set) -> dict:
    """CNY per 1 unit of each currency. Empty dict when the fetch fails."""
    if not currencies:
        return {}
    try:
        with urllib.request.urlopen(RATE_API, timeout=TIMEOUT) as resp:
            data = json.load(resp)
    except Exception as e:
        print(f"[fx] live rate fetch failed: {e}", file=sys.stderr)
        return {}

    if data.get("result") != "success":
        print(f"[fx] rate API returned: {data.get('result')}", file=sys.stderr)
        return {}

    rates = {}
    for currency in currencies:
        per_cny = data.get("rates", {}).get(currency)
        if per_cny:
            # API gives units-of-currency per CNY; we want CNY per unit.
            rates[currency] = 1 / per_cny
    updated = data.get("time_last_update_utc", "unknown")
    print(f"[fx] live rates ({updated}): "
          + ", ".join(f"1 {c} = {r:.4f} CNY" for c, r in sorted(rates.items())),
          file=sys.stderr)
    return rates


def parse_rate_overrides(args: list) -> dict:
    """Parse repeated --rate CUR=VALUE flags."""
    overrides = {}
    for i, arg in enumerate(args):
        if arg == "--rate" and i + 1 < len(args):
            currency, _, value = args[i + 1].partition("=")
            try:
                overrides[currency.strip().upper()] = float(value)
            except ValueError:
                print(f"[fx] ignoring malformed --rate {args[i + 1]}", file=sys.stderr)
    return overrides


def main():
    args = [a for a in sys.argv[1:]]
    if not args:
        print(__doc__, file=sys.stderr)
        sys.exit(1)

    overrides = parse_rate_overrides(args)
    positional = []
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg == "--rate":
            skip_next = True
            continue
        positional.append(arg)

    input_path = positional[0]
    output_path = positional[1] if len(positional) > 1 else None

    if input_path == "-":
        data = json.load(sys.stdin)
    else:
        with open(input_path, "r", encoding="utf-8") as f:
            data = json.load(f)

    transactions = data["transactions"] if isinstance(data, dict) else data
    config = load_config()

    targets = [t for t in transactions
               if fx.get_account_currency(t.get("account", ""), config) != fx.CNY]
    if not targets:
        print("[fx] no non-CNY transactions, nothing to do", file=sys.stderr)
    else:
        currencies = {fx.get_account_currency(t["account"], config) for t in targets}
        rates = fetch_live_rates(currencies - set(overrides)) if currencies - set(overrides) else {}
        rates.update(overrides)

        converted = 0
        for tx in targets:
            currency = fx.get_account_currency(tx["account"], config)
            rate = rates.get(currency)
            if rate is None:
                print(f"[fx] no rate for {currency}, leaving "
                      f"{tx.get('description', '')!r} as-is", file=sys.stderr)
                continue
            source = "manual" if currency in overrides else "live"
            if fx.apply_fx(tx, config, rate=rate, rate_source=source):
                converted += 1
        print(f"[fx] refreshed {converted}/{len(targets)} transaction(s)", file=sys.stderr)

    out = json.dumps(data, ensure_ascii=False, indent=2)
    if output_path:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(out + "\n")
        print(f"[fx] written to {output_path}", file=sys.stderr)
    else:
        print(out)


if __name__ == "__main__":
    main()
