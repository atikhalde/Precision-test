#!/usr/bin/env python3
"""Build an NSE equity symbol list (yfinance-style, no .NS suffix).

Tries several public sources in order and writes the first one that yields a
plausible universe. Run it where the internet is unrestricted (a GitHub runner
or a VPS); it is not part of the scanner.

Usage:
    python tools/build_nse_universe.py --out symbols_nse.txt [--min-symbols 500]
"""
from __future__ import annotations

import argparse
import csv
import gzip
import io
import re
import sys

import requests

UA = {"User-Agent": "Mozilla/5.0 (compatible; Precision-C-universe/1.0)"}
SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9&\-]{0,19}$")


def _clean(symbols: list[str]) -> list[str]:
    out: list[str] = []
    for raw in symbols:
        symbol = (raw or "").strip().upper()
        if not symbol or not SYMBOL_RE.match(symbol):
            continue
        if symbol.endswith("-RE") or symbol.endswith("-BE"):
            continue
        out.append(symbol)
    return sorted(dict.fromkeys(out))


def from_nse_archives() -> list[str]:
    """Official NSE equity list (EQ series)."""
    url = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"
    response = requests.get(url, timeout=30, headers=UA)
    response.raise_for_status()
    reader = csv.DictReader(io.StringIO(response.text))
    return _clean([row.get("SYMBOL", "") for row in reader
                   if (row.get(" SERIES") or row.get("SERIES") or "").strip() == "EQ"])


def from_kite() -> list[str]:
    """Zerodha public instrument dump (no credentials needed)."""
    response = requests.get("https://api.kite.trade/instruments/NSE", timeout=45, headers=UA)
    response.raise_for_status()
    reader = csv.DictReader(io.StringIO(response.text))
    return _clean([row.get("tradingsymbol", "") for row in reader
                   if (row.get("segment") or "").upper() == "NSE"
                   and (row.get("instrument_type") or "").upper() == "EQ"])


def from_upstox() -> list[str]:
    """Upstox public instrument dump (gzipped CSV)."""
    response = requests.get("https://assets.upstox.com/market-quote/instruments/exchange/NSE.csv.gz",
                            timeout=45, headers=UA)
    response.raise_for_status()
    text = gzip.decompress(response.content).decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    symbols = []
    for row in reader:
        kind = (row.get("instrument_type") or row.get("instrumentType") or "").upper()
        if kind in {"EQ", "EQUITY"}:
            symbols.append((row.get("tradingsymbol") or row.get("trading_symbol")
                            or row.get("symbol") or "").split(" ")[0])
    return _clean(symbols)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="symbols_nse.txt")
    ap.add_argument("--min-symbols", type=int, default=500)
    args = ap.parse_args()

    for name, fetch in (("nse-archives", from_nse_archives),
                        ("kite", from_kite),
                        ("upstox", from_upstox)):
        try:
            symbols = fetch()
        except Exception as exc:
            print(f"REPORT| universe source {name}: FAILED {type(exc).__name__}: {exc}")
            continue
        print(f"source {name}: {len(symbols)} EQ symbols")
        if len(symbols) >= args.min_symbols:
            with open(args.out, "w", encoding="utf-8") as handle:
                handle.write(f"# NSE equity universe from {name} "
                             f"({len(symbols)} symbols, generated {__import__('datetime').date.today()})\n")
                handle.write("\n".join(symbols) + "\n")
            print(f"wrote {args.out}")
            print(f"REPORT| universe source {name}: OK {len(symbols)} EQ symbols written to {args.out}")
            return 0
        print(f"REPORT| universe source {name}: only {len(symbols)} symbols "
              f"(< {args.min_symbols}); trying the next source")

    print("all sources failed")
    print("REPORT| universe: ALL SOURCES FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(main())
