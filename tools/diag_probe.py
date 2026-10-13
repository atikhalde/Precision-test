#!/usr/bin/env python3
"""Diagnostic probe for the Entry C scanner.

Read-only: never sends Telegram alerts, never touches the live ledger. It
reports what the scanner can see right now, finds which symbols are in an
active 26-week breakout cycle, and replays the real ``scan_pass`` at the C
candidate window against today's data — including an emulated provider delay.

Lines starting with ``REPORT|`` are the machine-readable headlines; the
diagnose-entry-c workflow turns them into run annotations. GitHub stores a
limited number of annotations per step, so the probe emits at most ten lines.

Usage:
    python tools/diag_probe.py [--as-of HH:MM] [--quote-lag SECONDS]
                               [--post-close-at HH:MM] [--feed-probe-symbols N]
"""
from __future__ import annotations

import argparse
import logging
import math
import os
import sys
import time as time_module
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import entry_c as ec  # noqa: E402

REPORT = "REPORT|"


class _Capture(logging.Handler):
    """Collect the scanner's own log lines so the replay verdict is quotable."""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(record.getMessage())
        except Exception:  # pragma: no cover - defensive
            pass

    def tally(self) -> str:
        summary = next((line for line in self.lines if line.startswith("Scan complete")), "")
        notable = [line for line in self.lines
                   if "delayed by the provider" in line or "unusable" in line
                   or "no yfinance intraday bars" in line]
        parts = [summary]
        if notable:
            parts.append(f"{len(notable)} quote warning(s)")
        return " | ".join(part for part in parts if part)


class _RecordingTelegram(ec.Telegram):
    """Dry-run notifier that still records what would have been sent."""

    def __init__(self) -> None:
        super().__init__("", "", dry_run=True)
        self.messages: list[str] = []

    def send(self, text: str) -> bool:
        super().send(text)
        self.messages.append(text)
        return True


def report(text: str) -> None:
    print(f"{REPORT} {text}", flush=True)


def section(title: str) -> None:
    print(f"\n{'=' * 72}\n== {title}\n{'=' * 72}", flush=True)


def _hhmm(value: str) -> tuple[int, int]:
    hh, mm = value.split(":", 1)
    return int(hh), int(mm)


def env_report(now: datetime, symbol_count: int) -> None:
    section("ENV / CONFIG AS THE WORKFLOW RESOLVES IT")
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat = os.getenv("TELEGRAM_CHAT_ID", "")
    dry_run = os.getenv("DRY_RUN", "0").strip().lower() in {"1", "true", "yes"}
    print(f"now (UTC)            : {now.astimezone(timezone.utc).isoformat(timespec='seconds')}")
    print(f"now (IST)            : {now.isoformat(timespec='seconds')}")
    print(f"market open now      : {ec._is_market_time(now)}  (NSE 09:15-15:30 IST, Mon-Fri)")
    print(f"C_SIGNAL_START       : {os.getenv('C_SIGNAL_START', '<unset>')!r}")
    print(f"C_POST_CLOSE_UNTIL   : {os.getenv('C_POST_CLOSE_UNTIL', '<unset>')!r}"
          f" -> {ec.post_close_until_from_env() or 'disabled'}")
    print(f"MAX_QUOTE_AGE_SECONDS: {os.getenv('MAX_QUOTE_AGE_SECONDS', '<unset>')!r}"
          f" -> {ec.quote_age_limit()}s (hard limit {ec.HARD_QUOTE_STALE_SECONDS}s)")
    try:
        import yfinance
        print(f"yfinance             : {yfinance.__version__}")
    except Exception as exc:  # pragma: no cover
        print(f"yfinance             : IMPORT FAILED {exc}")
    # Never print the values; presence and length only.
    print(f"TELEGRAM_BOT_TOKEN   : {'SET' if token else 'MISSING'} (len={len(token)})")
    print(f"TELEGRAM_CHAT_ID     : {'SET' if chat else 'MISSING'} (len={len(chat)})")
    report(f"config: start={os.getenv('C_SIGNAL_START', '15:15')} "
           f"close_until={ec.post_close_until_from_env() or 'disabled'} "
           f"age_limit={ec.quote_age_limit()}s hard_limit={ec.HARD_QUOTE_STALE_SECONDS}s "
           f"token={'SET' if token else 'MISSING'} chat={'SET' if chat else 'MISSING'} "
           f"dry_run={dry_run} symbols={symbol_count}")


def feed_probe(symbols: list[str], now: datetime, max_symbols: int) -> None:
    """Raw chart-API delay check for a *sample* of the watchlist."""
    section(f"RAW YAHOO CHART-API PROBE (feed delay, first {max_symbols} symbols)")
    import requests
    for symbol in symbols[:max_symbols]:
        ticker = ec.yahoo_ticker(symbol)
        try:
            response = requests.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
                f"?interval=1m&range=1d", timeout=20, headers={"User-Agent": "Mozilla/5.0"})
            payload = response.json()["chart"]["result"][0]
            meta = payload["meta"]
            stamps = payload.get("timestamp") or []
            closes = (payload["indicators"]["quote"][0].get("close") or [])
            last_i = next((i for i in range(len(stamps) - 1, -1, -1)
                           if i < len(closes) and closes[i] is not None), None)
            print(f"{symbol}: price={meta.get('regularMarketPrice')} "
                  f"bars={len(stamps)}", end="")
            if last_i is not None:
                last_bar = datetime.fromtimestamp(stamps[last_i], ec.IST)
                age = (now - last_bar).total_seconds()
                print(f"; last 1m bar {last_bar.strftime('%H:%M')} IST, age {age / 60:.1f} min")
                report(f"feed delay sample ({symbol}): last 1m bar {last_bar.strftime('%H:%M')} IST, "
                       f"age {age / 60:.1f} min")
            else:
                print("; no bar data")
        except requests.RequestException as exc:
            print(f"{symbol} ({ticker}): probe error: {exc!r}")


def _fmt_bar(bar: ec.Bar | None) -> str:
    if bar is None:
        return "<none>"
    return (f"{str(bar.time)[:10]} O={bar.open:.2f} H={bar.high:.2f} "
            f"L={bar.low:.2f} C={bar.close:.2f} V={bar.volume:.0f}")


def breakout_scan(symbols: list[str], now: datetime) -> None:
    """Daily-bars pass over the whole watchlist: who is in a live cycle?

    The downloaded frames are also seeded into entry_c's per-session cache so
    the replays below reuse them instead re-downloading the whole universe.
    """
    section("UNIVERSE 26-WEEK BREAKOUT SCAN (daily bars)")
    params = ec.OBParams()
    tickers = [ec.yahoo_ticker(s) for s in symbols]
    start = time_module.monotonic()
    daily = ec._download(tickers, ec.DEFAULT_LOOKBACK, "1d")
    elapsed = time_module.monotonic() - start
    print(f"daily 2y download: {len(daily)}/{len(tickers)} tickers in {elapsed:.1f}s")
    report(f"daily download: {len(daily)}/{len(tickers)} tickers in "
           f"{elapsed:.1f}s ({elapsed / max(len(tickers), 1) * 1000:.0f} ms/symbol)")

    # Seed the scanner's own session cache so scan_pass replays reuse it.
    ec._DAILY_CACHE_DAY = now.date()
    ec._DAILY_CACHE = dict(daily)

    today = now.date().isoformat()
    cycles: list[str] = []
    candles: list[str] = []
    start = time_module.monotonic()
    for symbol in symbols:
        ticker = ec.yahoo_ticker(symbol)
        all_daily = ec.daily_bars_from_frame(daily.get(ticker))
        if not all_daily:
            continue
        closed = [b for b in all_daily if str(b.time)[:10] < today]
        today_row = next((b for b in all_daily if str(b.time)[:10] == today), None)
        combined = closed + ([today_row] if today_row else [])
        breakout = ec.derive_active_26w_breakout(combined)
        if breakout is None:
            continue
        born = ec.first_ob_after_breakout(closed, breakout, params)
        state = "first OB already born" if born else "open for candidates"
        candle_ok = today_row and ec.is_potential_origin(today_row, breakout, params)
        cycles.append(symbol)
        print(f"  {symbol:14s} breakout={breakout.session} level={breakout.level:.2f} "
              f"week_close={breakout.close:.2f} {state} candle={_fmt_bar(today_row)}"
              f" candidate_now={bool(candle_ok)}")
        if born is None:
            candles.append(symbol)
    elapsed = time_module.monotonic() - start
    print(f"\nprocessing: {len(symbols)} symbols in {elapsed:.1f}s")
    cycles_text = ",".join(cycles[:10]) or "none"
    report(f"universe: {len(symbols)} symbols, {len(cycles)} in an active 26W breakout "
           f"[{cycles_text}]; {len(candles)} open for a C candidate today "
           f"[{','.join(candles[:10]) or 'none'}]")


def replay_scan(symbols: list[str], now: datetime, quote_lag: int, label: str,
                age_limit: int | None = None) -> None:
    """Run the *real* scan_pass at `now`, optionally emulating provider delay."""
    section(f"REPLAY scan_pass @ {now.isoformat(timespec='seconds')} IST "
            f"(emulated quote lag {quote_lag}s, age limit "
            f"{age_limit if age_limit else ec.quote_age_limit()}s) — {label}")
    real_download = ec._download
    patched_env = False
    if age_limit:
        os.environ["MAX_QUOTE_AGE_SECONDS"] = str(age_limit)
        patched_env = True

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

    capture = _Capture()
    logging.getLogger().addHandler(capture)
    start = time_module.monotonic()
    try:
        state = {"schema": 1, "candidates": {}, "alerts_sent": {}}
        notifier = _RecordingTelegram()
        changed = ec.scan_pass(symbols, state, notifier, now=now,
                              candidate_start=ec._time_from_env("C_SIGNAL_START",
                                                                ec.DEFAULT_C_START))
        elapsed = time_module.monotonic() - start
        keys = list(state["candidates"])
        print(f"\nscan_pass completed in {elapsed:.1f}s. state_changed={changed}")
        print(f"candidates that would be tracked: {keys}")
        shown = ",".join(k.split("|")[1] for k in keys[:8])
        if len(keys) > 8:
            shown += f" (+{len(keys) - 8} more)"
        report(f"replay({label}): OK {elapsed:.1f}s alerts={len(notifier.messages)} "
               f"candidates={len(keys)} [{shown or 'none'}] | {capture.tally()}")
    except Exception as exc:
        print("\n>>> scan_pass RAISED:")
        traceback.print_exc()
        report(f"replay({label}): RAISED {exc!r} | {capture.tally()}")
    finally:
        logging.getLogger().removeHandler(capture)
        ec._download = real_download  # type: ignore[assignment]
        if patched_env:
            os.environ.pop("MAX_QUOTE_AGE_SECONDS", None)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--as-of", help="HH:MM IST to replay the C window at (default: now)")
    ap.add_argument("--quote-lag", type=int, default=0,
                    help="emulate a provider that lags N seconds behind the clock")
    ap.add_argument("--post-close-at", default="",
                    help="HH:MM IST for the settled-candle replay (blank to skip)")
    ap.add_argument("--feed-probe-symbols", type=int, default=3,
                    help="raw chart-API delay sampling size (default 3)")
    ap.add_argument("--symbols-file", default=str(ROOT / "symbols.txt"))
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    symbols = ec.load_symbols(Path(args.symbols_file))
    now = datetime.now(ec.IST)
    env_report(now, len(symbols))
    print(f"symbols              : {len(symbols)} "
          f"({', '.join(symbols[:8])}{' ...' if len(symbols) > 8 else ''})")

    feed_probe(symbols, now, max(0, args.feed_probe_symbols))
    breakout_scan(symbols, now)

    if args.as_of:
        hh, mm = _hhmm(args.as_of)
        replay_now = now.replace(hour=hh, minute=mm, second=30, microsecond=0)
        print(f"\nreplay clock moved from {now.isoformat(timespec='seconds')} "
              f"to {replay_now.isoformat(timespec='seconds')} IST")
    else:
        replay_now = now
    replay_scan(symbols, replay_now, 0, "ideal feed, no provider delay")
    if args.quote_lag:
        replay_scan(symbols, replay_now, args.quote_lag,
                    f"provider lags {args.quote_lag}s as the live 15:2x run saw it",
                    age_limit=300)
        replay_scan(symbols, replay_now, args.quote_lag,
                    f"provider lags {args.quote_lag}s with the deployed limit",
                    age_limit=None)
    if args.post_close_at:
        hh, mm = _hhmm(args.post_close_at)
        replay_scan(symbols, now.replace(hour=hh, minute=mm, second=0, microsecond=0),
                    0, f"settled-candle pass at {args.post_close_at} IST")
    return 0


if __name__ == "__main__":
    sys.exit(main())
