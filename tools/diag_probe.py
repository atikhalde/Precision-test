#!/usr/bin/env python3
"""Diagnostic probe for the Entry C scanner.

Read-only: never sends Telegram alerts, never touches the live ledger. It
reports what the scanner can see right now, why each symbol is or is not
alertable, and replays the C window against today's data.

Lines starting with ``REPORT|`` are the machine-readable headlines; the
diagnose-entry-c workflow turns them into run annotations.

Usage:
    python tools/diag_probe.py [--as-of HH:MM] [--quote-lag SECONDS]
                               [--post-close-at HH:MM]
"""
from __future__ import annotations

import argparse
import logging
import math
import os
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import entry_c as ec  # noqa: E402

REPORT = "REPORT|"
# GitHub stores a limited number of annotations per step, so the report lines
# are consolidated: config (1) + one per symbol + one per replay.
_MAX_REPORT_LINES = 10


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


def env_report(now: datetime) -> None:
    section("ENV / CONFIG AS THE WORKFLOW RESOLVES IT")
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat = os.getenv("TELEGRAM_CHAT_ID", "")
    dry_run = os.getenv("DRY_RUN", "0").strip().lower() in {"1", "true", "yes"}
    print(f"now (UTC)            : {now.astimezone(timezone.utc).isoformat(timespec='seconds')}")
    print(f"now (IST)            : {now.isoformat(timespec='seconds')}")
    print(f"market open now      : {ec._is_market_time(now)}  (NSE 09:15-15:30 IST, Mon-Fri)")
    print(f"post-close window    : {ec._is_post_close_time(now, ec.post_close_until_from_env())}")
    print(f"C_SIGNAL_START       : {os.getenv('C_SIGNAL_START', '<unset>')!r}")
    print(f"C_POST_CLOSE_UNTIL   : {os.getenv('C_POST_CLOSE_UNTIL', '<unset>')!r}"
          f" -> {ec.post_close_until_from_env() or 'disabled'}")
    print(f"MAX_QUOTE_AGE_SECONDS: {os.getenv('MAX_QUOTE_AGE_SECONDS', '<unset>')!r}"
          f" -> {ec.quote_age_limit()}s (hard limit {ec.HARD_QUOTE_STALE_SECONDS}s)")
    print(f"ACCOUNT_CAPITAL_INR  : {os.getenv('ACCOUNT_CAPITAL_INR', '<unset>')!r}")
    try:
        import yfinance
        print(f"yfinance             : {yfinance.__version__}")
    except Exception as exc:  # pragma: no cover
        print(f"yfinance             : IMPORT FAILED {exc}")
    # Never print the values; presence and length only.
    print(f"TELEGRAM_BOT_TOKEN   : {'SET' if token else 'MISSING'} (len={len(token)})")
    print(f"TELEGRAM_CHAT_ID     : {'SET' if chat else 'MISSING'} (len={len(chat)})")
    symbols = ec.load_symbols(Path(os.getenv("SYMBOLS_FILE", str(ROOT / "symbols.txt"))))
    report(f"config: start={os.getenv('C_SIGNAL_START', '15:15')} "
           f"close_until={ec.post_close_until_from_env() or 'disabled'} "
           f"age_limit={ec.quote_age_limit()}s hard_limit={ec.HARD_QUOTE_STALE_SECONDS}s "
           f"token={'SET' if token else 'MISSING'} chat={'SET' if chat else 'MISSING'} "
           f"dry_run={dry_run} symbols={len(symbols)}")


def feed_probe(symbols: list[str], now: datetime) -> dict[str, tuple[datetime, float]]:
    """Raw chart-API view: how delayed is the feed for each symbol right now?"""
    section("RAW YAHOO CHART-API PROBE (feed delay)")
    import requests
    delays: dict[str, tuple[datetime, float]] = {}
    for symbol in symbols:
        ticker = ec.yahoo_ticker(symbol)
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
               f"?interval=1m&range=1d")
        try:
            response = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
            payload = response.json()["chart"]["result"][0]
            meta = payload["meta"]
            stamps = payload.get("timestamp") or []
            closes = (payload["indicators"]["quote"][0].get("close") or [])
            last_i = next((i for i in range(len(stamps) - 1, -1, -1)
                           if i < len(closes) and closes[i] is not None), None)
            rmt = meta.get("regularMarketTime")
            print(f"\n{symbol} ({ticker})")
            print(f"  exchange             : {meta.get('fullExchangeName')} "
                  f"tz={meta.get('exchangeTimezoneName')}")
            print(f"  regularMarketPrice   : {meta.get('regularMarketPrice')}")
            if rmt:
                rmt_dt = datetime.fromtimestamp(rmt, ec.IST)
                print(f"  regularMarketTime    : {rmt_dt.isoformat(timespec='seconds')} "
                      f"(age {(now - rmt_dt).total_seconds():.0f}s)")
            if last_i is not None:
                last_bar = datetime.fromtimestamp(stamps[last_i], ec.IST)
                age = (now - last_bar).total_seconds()
                delays[symbol] = (last_bar, age)
                print(f"  last 1m bar w/ data  : {last_bar.isoformat(timespec='seconds')} "
                      f"(age {age:.0f}s)")
            print(f"  bars returned        : {len(stamps)}")
        except Exception as exc:
            print(f"\n{symbol} ({ticker}): probe failed: {exc!r}")
            delays[symbol] = (now, float("nan"))
    return delays


def _fmt_bar(bar: ec.Bar | None) -> str:
    if bar is None:
        return "<none>"
    return (f"{str(bar.time)[:10]} O={bar.open:.2f} H={bar.high:.2f} "
            f"L={bar.low:.2f} C={bar.close:.2f} V={bar.volume:.0f}")


def strategy_report(symbols: list[str], now: datetime,
                    delays: dict[str, tuple[datetime, float]] | None = None
                    ) -> dict[str, object]:
    """Per-symbol view of the exact C decision inputs."""
    section("PER-SYMBOL C DECISION INPUTS")
    delays = delays or {}
    params = ec.OBParams()
    tickers = [ec.yahoo_ticker(s) for s in symbols]
    daily = ec._download(tickers, ec.DEFAULT_LOOKBACK, "1d")
    intraday = ec._download(tickers, "1d", "1m")
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
                status, age = ec.quote_status(snap, now)
                print(f"  intraday snapshot : {_fmt_bar(snap.bar)}")
                print(f"  newest 1m bar     : {snap.as_of.isoformat(timespec='seconds')} "
                      f"(age {age:.0f}s -> {status})")

            combined = closed + ([snap.bar] if snap else ([today_bar] if today_bar else []))
            breakout = ec.derive_active_26w_breakout(combined)
            candle = snap.bar if snap else today_bar
            if breakout is None:
                print("  active 26W breakout: NONE -> C candidate impossible for this symbol")
                verdict = (f"no active 26-week breakout (last close "
                           f"{combined[-1].close:.2f}) -> no C candidate possible")
            else:
                print(f"  active 26W breakout: session={breakout.session} "
                      f"level={breakout.level:.2f} week_close={breakout.close:.2f}")
                if candle is None:
                    print("  candidate candle  : NONE")
                    verdict = f"breakout {breakout.session} @ {breakout.level:.2f}; no candle"
                else:
                    rng = max(candle.high - candle.low, params.mintick)
                    bearish = candle.close < candle.open
                    neutral = (params.allow_neutral
                               and abs(candle.close - candle.open) / rng <= params.neutral_body)
                    above = candle.close > breakout.level
                    zone = candle.open > candle.low
                    qualifies = ec.is_potential_origin(candle, breakout, params)
                    print(f"  candidate candle  : {_fmt_bar(candle)}")
                    print(f"    bearish={bearish} neutral={neutral} "
                          f"close>level={above} open>low={zone}")
                    print(f"    -> is_potential_origin = {qualifies}")
                    verdict = (f"breakout {breakout.session} @ {breakout.level:.2f}; "
                               f"candle {_fmt_bar(candle)}; candidate={qualifies}")
                first_ob = ec.first_ob_after_breakout(closed, breakout, params)
                print(f"  first OB already born: "
                      f"{first_ob.zone.born_session if first_ob else 'no'}")
            last_bar, age = delays.get(symbol, (None, float("nan")))
            delay_text = ("feed_delay=unknown" if last_bar is None or math.isnan(age)
                          else f"feed_delay={age / 60:.1f}min last_bar="
                               f"{last_bar.strftime('%H:%M')}")
            report(f"{symbol}: {delay_text}; {verdict}")
            out[symbol] = {
                "daily_bars": len(all_daily),
                "today_row": _fmt_bar(today_bar),
                "intraday_age_s": (now - snap.as_of).total_seconds() if snap else None,
                "breakout": (breakout.session, breakout.level) if breakout else None,
            }
        except Exception:
            print(f"  ERROR for {symbol}:\n{traceback.format_exc()}")
            report(f"{symbol}: probe error: {sys.exc_info()[1]!r}")
    return out


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
    try:
        state = {"schema": 1, "candidates": {}, "alerts_sent": {}}
        notifier = _RecordingTelegram()
        changed = ec.scan_pass(symbols, state, notifier, now=now,
                              candidate_start=ec._time_from_env("C_SIGNAL_START",
                                                                ec.DEFAULT_C_START))
        print(f"\nscan_pass completed. state_changed={changed}")
        print(f"candidates that would be tracked: {list(state['candidates'])}")
        report(f"replay({label}): OK state_changed={changed} "
               f"candidates={list(state['candidates']) or 'none'} "
               f"alerts={len(notifier.messages)} | {capture.tally()}")
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
    ap.add_argument("--symbols-file", default=str(ROOT / "symbols.txt"))
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    symbols = ec.load_symbols(Path(args.symbols_file))
    now = datetime.now(ec.IST)
    env_report(now)
    print(f"symbols              : {symbols}")

    delays = feed_probe(symbols, now)
    strategy_report(symbols, now, delays)

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
                    age_limit=ec.quote_age_limit())
    if args.post_close_at:
        hh, mm = _hhmm(args.post_close_at)
        replay_scan(symbols, now.replace(hour=hh, minute=mm, second=0, microsecond=0),
                    0, f"settled-candle pass at {args.post_close_at} IST")
    return 0


if __name__ == "__main__":
    sys.exit(main())
