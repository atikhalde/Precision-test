#!/usr/bin/env python3
"""Diagnostic probe for the Entry C scanner.

Read-only: never sends Telegram alerts, never touches the live ledger. It only
reports what the scanner can see and why a symbol is or is not alertable.

Usage:
    python tools/diag_probe.py [--as-of HH:MM] [--quote-lag SECONDS]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import entry_c as ec  # noqa: E402


def _hhmm(value: str) -> tuple[int, int]:
    hh, mm = value.split(":", 1)
    return int(hh), int(mm)


def section(title: str) -> None:
    print(f"\n{'=' * 72}\n== {title}\n{'=' * 72}", flush=True)


def env_report(now: datetime) -> None:
    section("ENV / CONFIG AS THE WORKFLOW RESOLVES IT")
    print(f"now (UTC)            : {now.astimezone(timezone.utc).isoformat(timespec='seconds')}")
    print(f"now (IST)            : {now.isoformat(timespec='seconds')}")
    print(f"weekday              : {now.strftime('%A')} ({now.weekday()})")
    print(f"market open now      : {ec._is_market_time(now)}  (NSE 09:15-15:30 IST, Mon-Fri)")
    print(f"DRY_RUN              : {os.getenv('DRY_RUN', '<unset>')!r}"
          f"  -> scanner dry run: {ec.os.getenv('DRY_RUN', '0').strip().lower() in {'1', 'true', 'yes'}}")
    print(f"C_SIGNAL_START       : {os.getenv('C_SIGNAL_START', '<unset>')!r}")
    print(f"MAX_QUOTE_AGE_SECONDS: {os.getenv('MAX_QUOTE_AGE_SECONDS', '<unset>')!r}")
    print(f"ACCOUNT_CAPITAL_INR  : {os.getenv('ACCOUNT_CAPITAL_INR', '<unset>')!r}")
    print(f"RISK_PER_TRADE_PCT   : {os.getenv('RISK_PER_TRADE_PCT', '<unset>')!r}")
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat = os.getenv("TELEGRAM_CHAT_ID", "")
    # Never print the values; presence and length only.
    print(f"TELEGRAM_BOT_TOKEN   : {'SET' if token else 'MISSING'} (len={len(token)})")
    print(f"TELEGRAM_CHAT_ID     : {'SET' if chat else 'MISSING'} (len={len(chat)})")
    try:
        import yfinance
        print(f"yfinance             : {yfinance.__version__}")
    except Exception as exc:  # pragma: no cover
        print(f"yfinance             : IMPORT FAILED {exc}")


def feed_probe(symbols: list[str], now: datetime) -> None:
    """Raw chart-API view: how delayed is the feed for each symbol right now?"""
    section("RAW YAHOO CHART-API PROBE (feed delay)")
    import requests
    for symbol in symbols:
        ticker = ec.yahoo_ticker(symbol)
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
               f"?interval=1m&range=1d")
        try:
            response = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
            payload = response.json()["chart"]["result"][0]
            meta = payload["meta"]
            stamps = payload.get("timestamp") or []
            quotes = payload["indicators"]["quote"][0]
            closes = quotes.get("close") or []
            last_i = None
            for i in range(len(stamps) - 1, -1, -1):
                if i < len(closes) and closes[i] is not None:
                    last_i = i
                    break
            rmt = meta.get("regularMarketTime")
            print(f"\n{symbol} ({ticker})")
            print(f"  exchange             : {meta.get('fullExchangeName')} tz={meta.get('exchangeTimezoneName')}")
            print(f"  regularMarketPrice   : {meta.get('regularMarketPrice')}")
            print(f"  regularMarketTime    : {datetime.fromtimestamp(rmt, ec.IST).isoformat(timespec='seconds') if rmt else None}"
                  f"  (age {(now - datetime.fromtimestamp(rmt, ec.IST)).total_seconds():.0f}s)" if rmt else "")
            if last_i is not None:
                last_bar = datetime.fromtimestamp(stamps[last_i], ec.IST)
                print(f"  last 1m bar w/ data  : {last_bar.isoformat(timespec='seconds')}"
                      f"  (age {(now - last_bar).total_seconds():.0f}s)")
            print(f"  bars returned        : {len(stamps)} (last timestamp "
                  f"{datetime.fromtimestamp(stamps[-1], ec.IST).isoformat(timespec='seconds') if stamps else '-'})")
        except Exception as exc:
            print(f"\n{symbol} ({ticker}): probe failed: {exc!r}")


def _fmt_bar(bar: ec.Bar | None) -> str:
    if bar is None:
        return "<none>"
    return (f"{str(bar.time)[:10]} O={bar.open:.2f} H={bar.high:.2f} "
            f"L={bar.low:.2f} C={bar.close:.2f} V={bar.volume:.0f}")


def strategy_report(symbols: list[str], now: datetime) -> dict[str, object]:
    """Per-symbol view of the exact C decision inputs."""
    section("PER-SYMBOL C DECISION INPUTS")
    params = ec.OBParams()
    tickers = [ec.yahoo_ticker(s) for s in symbols]
    daily = ec._download(tickers, ec.DEFAULT_LOOKBACK, "1d")
    intraday = ec._download(tickers, "1d", "1m")
    max_age = int(os.getenv("MAX_QUOTE_AGE_SECONDS", "300"))
    out: dict[str, object] = {}
    for symbol in symbols:
        ticker = ec.yahoo_ticker(symbol)
        print(f"\n--- {symbol} ({ticker})")
        try:
            all_daily = ec.daily_bars_from_frame(daily.get(ticker))
            print(f"  daily bars        : {len(all_daily)}")
            if all_daily:
                print(f"  first/last session: {all_daily[0].session} .. {all_daily[-1].session}")
                for bar in all_daily[-4:]:
                    print(f"    {_fmt_bar(bar)}")
            today = now.date().isoformat()
            closed = [b for b in all_daily if str(b.time)[:10] < today]
            today_bar = next((b for b in all_daily if str(b.time)[:10] == today), None)
            print(f"  today's daily row : {_fmt_bar(today_bar)}")

            snap = ec.intraday_snapshot(intraday.get(ticker), now.date(), now)
            if snap is None:
                print("  intraday snapshot : NONE (no usable 1m bars for this session)")
            else:
                age = (now - snap.as_of).total_seconds()
                print(f"  intraday snapshot : {_fmt_bar(snap.bar)}")
                print(f"  newest 1m bar     : {snap.as_of.isoformat(timespec='seconds')} "
                      f"(age {age:.0f}s; gate {max_age}s -> fresh={ec._is_fresh(snap, now, max_age)})")

            combined = closed + ([snap.bar] if snap else ([today_bar] if today_bar else []))
            breakout = ec.derive_active_26w_breakout(combined)
            if breakout is None:
                print("  active 26W breakout: NONE -> C candidate impossible for this symbol")
            else:
                print(f"  active 26W breakout: session={breakout.session} level={breakout.level:.2f} "
                      f"week_close={breakout.close:.2f} week_index={breakout.week_index}")
                candle = snap.bar if snap else today_bar
                if candle is None:
                    print("  candidate candle  : NONE")
                else:
                    rng = max(candle.high - candle.low, params.mintick)
                    bearish = candle.close < candle.open
                    neutral = (params.allow_neutral
                               and abs(candle.close - candle.open) / rng <= params.neutral_body)
                    above = candle.close > breakout.level
                    zone = candle.open > candle.low
                    print(f"  candidate candle  : {_fmt_bar(candle)}")
                    print(f"    bearish={bearish} neutral={neutral} (neutral_body<={params.neutral_body})")
                    print(f"    close>level       : {above} ({candle.close:.2f} vs {breakout.level:.2f})")
                    print(f"    open>low (zone)   : {zone} ({candle.open:.2f} vs {candle.low:.2f})")
                    print(f"    -> is_potential_origin = {ec.is_potential_origin(candle, breakout, params)}")
                first_ob = ec.first_ob_after_breakout(closed, breakout, params)
                print(f"  first OB already born: "
                      f"{first_ob.zone.born_session if first_ob else 'no'}")
            out[symbol] = {
                "daily_bars": len(all_daily),
                "today_row": _fmt_bar(today_bar),
                "intraday_age_s": (now - snap.as_of).total_seconds() if snap else None,
                "breakout": (breakout.session, breakout.level) if breakout else None,
            }
        except Exception:
            print(f"  ERROR for {symbol}:\n{traceback.format_exc()}")
    return out


def replay_scan(symbols: list[str], now: datetime, quote_lag: int, label: str) -> None:
    """Run the *real* scan_pass at `now`, optionally emulating provider delay."""
    section(f"REPLAY scan_pass @ {now.isoformat(timespec='seconds')} IST "
            f"(emulated quote delay {quote_lag}s) — {label}")
    real_download = ec._download

    if quote_lag:
        def lagged(tickers: list[str], period: str, interval: str):
            frames = real_download(tickers, period, interval)
            if interval != "1m":
                return frames
            cutoff = now.astimezone(ec.IST).replace(tzinfo=None) - timedelta(seconds=quote_lag)
            trimmed = {}
            for ticker, frame in frames.items():
                try:
                    keep = [ts for ts in frame.index
                            if ec._index_time(ts).replace(tzinfo=None) <= cutoff]
                    if keep:
                        trimmed[ticker] = frame.loc[keep]
                except Exception:
                    continue
            return trimmed
        ec._download = lagged  # type: ignore[assignment]

    try:
        state = {"schema": 1, "candidates": {}, "alerts_sent": {}}
        notifier = ec.Telegram("", "", dry_run=True)
        changed = ec.scan_pass(symbols, state, notifier, now=now,
                              candidate_start=ec._time_from_env("C_SIGNAL_START", ec.DEFAULT_C_START))
        print(f"\nscan_pass completed. state_changed={changed}")
        print(f"candidates that would be tracked: {list(state['candidates'])}")
    except Exception:
        print("\n>>> scan_pass RAISED — this is the failure the live run hit:")
        traceback.print_exc()
    finally:
        ec._download = real_download  # type: ignore[assignment]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--as-of", help="HH:MM IST to replay the scan at (default: now)")
    ap.add_argument("--quote-lag", type=int, default=0,
                    help="emulate a provider that lags N seconds behind wall clock")
    ap.add_argument("--symbols-file", default=str(ROOT / "symbols.txt"))
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    symbols = ec.load_symbols(Path(args.symbols_file))
    now = datetime.now(ec.IST)
    env_report(now)
    print(f"symbols              : {symbols}")
    feed_probe(symbols, now)
    if args.as_of:
        hh, mm = _hhmm(args.as_of)
        replay_now = now.replace(hour=hh, minute=mm, second=30, microsecond=0)
        print(f"\nreplay clock moved from {now.isoformat(timespec='seconds')} "
              f"to {replay_now.isoformat(timespec='seconds')} IST")
    else:
        replay_now = now
    strategy_report(symbols, now)
    replay_scan(symbols, replay_now, 0, "ideal feed, no delay")
    if args.quote_lag:
        replay_scan(symbols, replay_now, args.quote_lag,
                    f"provider lags {args.quote_lag}s, i.e. what the live run actually saw")
    return 0


if __name__ == "__main__":
    sys.exit(main())
