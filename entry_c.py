#!/usr/bin/env python3
"""Entry C: an early, unconfirmed A-like candidate alert.

This is deliberately a C-only add-on. It does not implement or alter Entries A
or B. The exact order-block birth/confirmation logic is delegated to the
vendored `ob_precision.replay()` engine; C itself is the earlier speculative
alert on a potential origin candle, before a future displacement confirms it.

No orders are sent. Telegram messages are informational alerts only.
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import math
import os
import sys
import time as time_module
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ob_precision import Bar, OBParams, replay

ROOT = Path(__file__).resolve().parent
IST = ZoneInfo("Asia/Kolkata")
NSE_OPEN = time(9, 15)
NSE_CLOSE = time(15, 30)
DEFAULT_C_START = time(15, 15)  # near the candidate candle's close, but still provisional
# After the NSE close, today's candle is settled: it is re-checked so a missed
# 15:15-15:30 slot (or a delayed provider) cannot silently lose the session.
DEFAULT_POST_CLOSE_UNTIL = time(17, 0)
DEFAULT_POLL_SECONDS = 60
DEFAULT_LOOKBACK = "2y"
DEFAULT_STATE_FILE = ROOT / "state_c.json"
TIME_STOP_SESSIONS = 90
# Yahoo serves NSE intraday bars roughly 15 minutes behind the exchange, so a
# five-minute freshness gate can never pass inside the 15:15-15:30 IST window.
# The limit only controls when a delayed quote is reported; it is not a hard cut
# (see HARD_QUOTE_STALE_SECONDS and quote_status()).
DEFAULT_MAX_QUOTE_AGE_SECONDS = 1200
# Beyond this age a snapshot cannot be assumed to describe the current session.
HARD_QUOTE_STALE_SECONDS = 3600
# Retry a handful of symbols individually when a batch download drops them.
INDIVIDUAL_RETRY_LIMIT = 10

# Daily candles are static during the session; only refresh them on a new NSE
# session. Re-downloading two years of history every poll would invite throttling.
_DAILY_CACHE_DAY: date | None = None
_DAILY_CACHE: dict[str, Any] = {}

log = logging.getLogger("entry_c")


@dataclass(frozen=True)
class BreakoutCycle:
    session: str
    level: float
    close: float
    week_index: int


@dataclass(frozen=True)
class IntradaySnapshot:
    bar: Bar
    as_of: datetime


def as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value or "")[:10])
    except (TypeError, ValueError):
        return None


def session_of(bar: Bar) -> date | None:
    return as_date(bar.time)


def _finite_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def is_potential_origin(bar: Bar, breakout: BreakoutCycle,
                        params: OBParams | None = None) -> bool:
    """A known-now candle that *could* be the later-confirmed OB origin.

    C intentionally does not require a displacement: that is the extra,
    unconfirmed-risk part. The C candidate uses the same bearish/neutral test,
    strict close-above-level test and open-to-low zone geometry as the OB port.
    """
    params = params or OBParams()
    day = session_of(bar)
    brk_day = as_date(breakout.session)
    if day is None or brk_day is None or day < brk_day:
        return False
    rng = max(float(bar.high) - float(bar.low), params.mintick)
    bearish = float(bar.close) < float(bar.open)
    neutral = (params.allow_neutral
               and abs(float(bar.close) - float(bar.open)) / rng
               <= params.neutral_body)
    return bool(
        float(bar.close) > breakout.level
        and float(bar.open) > float(bar.low)  # non-zero Open-to-Low zone
        and (bearish or neutral)
    )


def _weekly_bars(bars: list[Bar]) -> list[dict[str, Any]]:
    """Monday-anchored weeks; weekly close is the last available session."""
    weeks: list[dict[str, Any]] = []
    for bar in sorted(bars, key=lambda b: str(b.time)):
        day = session_of(bar)
        if day is None or bar.high <= 0 or bar.close <= 0:
            continue
        start = day - timedelta(days=day.weekday())
        if weeks and weeks[-1]["start"] == start:
            week = weeks[-1]
            week["sessions"].append((day, float(bar.close)))
            week["high"] = max(week["high"], float(bar.high))
            week["close"] = float(bar.close)
        else:
            weeks.append({"start": start, "sessions": [(day, float(bar.close))],
                          "high": float(bar.high), "close": float(bar.close)})
    return weeks


def derive_active_26w_breakout(bars: list[Bar], len_short: int = 26,
                               lock_weeks: int = 26) -> BreakoutCycle | None:
    """Derive the active 26-week breakout from daily bars.

    For each week, the level is the maximum high of the prior `len_short`
    completed/available weekly rows. A cross is a weekly close strictly above
    that level. A cross-lock prevents a second cycle for `lock_weeks`; an
    active cycle is returned only while it remains inside that lock window.
    The caller may append today's *forming* daily bar, so the returned current
    week remains provisional until the bar/week closes.
    """
    len_short = max(1, int(len_short))
    lock_weeks = max(1, int(lock_weeks))
    weeks = _weekly_bars(bars)
    if len(weeks) < len_short + 1:
        return None

    last_cross: BreakoutCycle | None = None
    locked_from: int | None = None
    for k in range(len_short, len(weeks)):
        level = max(float(w["high"]) for w in weeks[k - len_short:k])
        if level <= 0 or float(weeks[k]["close"]) <= level:
            continue
        if locked_from is not None and k < locked_from + lock_weeks:
            continue
        cross_day = next((day for day, close in weeks[k]["sessions"]
                          if close > level), None)
        if cross_day is None:
            continue
        locked_from = k
        last_cross = BreakoutCycle(
            session=cross_day.isoformat(), level=level,
            close=float(weeks[k]["close"]), week_index=k)

    if last_cross is None or len(weeks) - 1 - last_cross.week_index >= lock_weeks:
        return None
    return last_cross


def first_ob_after_breakout(bars: list[Bar], breakout: BreakoutCycle,
                            params: OBParams | None = None):
    """Return the first OB birth on/after the cycle's breakout, if any.

    This uses the same `ob_precision.replay()` engine as the upstream scanner,
    including its displacement thresholds, origin choice, zone rules and
    duplicate handling. The first birth consumes the cycle even if its origin
    later fails the A-entry filters, matching the report's funnel.
    """
    params = params or OBParams()
    births = [event for event in replay(bars, params).events
              if event.kind == "ob"
              and event.zone.born_session >= breakout.session]
    births.sort(key=lambda event: (event.zone.born_session, event.bar_index))
    return births[0] if births else None


def candidate_resolution(candidate_session: str, breakout_session: str,
                          event: Any | None, bars_after_candidate: int,
                          max_origin_bars: int = 8,
                          final_candidate_valid: bool = True) -> tuple[str, str]:
    """Pure C lifecycle decision, exposed for deterministic unit tests."""
    if not final_candidate_valid:
        return "invalidated", "The candidate candle did not remain bearish/neutral above the 26W level at its close."
    if event is not None:
        if event.zone.origin_session == candidate_session:
            return "confirmed", f"Selected as the first OB's origin on {event.zone.born_session}."
        return "superseded", f"The cycle's first OB selected {event.zone.origin_session}, not this C candidate."
    if bars_after_candidate > max_origin_bars:
        return "expired", f"No qualifying displacement selected this candidate within {max_origin_bars} bars."
    if as_date(candidate_session) is None or as_date(breakout_session) is None:
        return "invalidated", "Candidate/breakout date could not be parsed."
    return "pending", "Waiting for a qualifying displacement; this remains speculative."


def candidate_id(symbol: str, breakout_session: str, candidate_session: str) -> str:
    return f"C|{symbol.upper()}|{breakout_session[:10]}|{candidate_session[:10]}"


def target_for_candidate(bars: list[Bar], breakout_session: str,
                         candidate_session: str, candidate_high: float) -> float | None:
    highs = [float(b.high) for b in bars
             if breakout_session <= str(b.time)[:10] <= candidate_session
             and float(b.high) > 0]
    if candidate_high > 0:
        highs.append(float(candidate_high))
    return max(highs) if highs else None


def _bar_final_candidate(bars: list[Bar], day: str, breakout: BreakoutCycle,
                         params: OBParams) -> bool | None:
    bar = next((b for b in bars if str(b.time)[:10] == day), None)
    if bar is None:
        return None
    return is_potential_origin(bar, breakout, params)


def load_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("state root is not an object")
    except FileNotFoundError:
        data = {}
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(f"Unable to read C ledger {path}; refusing to reset alert history") from exc
    data.setdefault("schema", 1)
    for field in ("candidates", "alerts_sent"):
        data.setdefault(field, {})
        if not isinstance(data[field], dict):
            raise RuntimeError(f"Invalid C ledger field {field}; refusing to reset alert history")
    return data


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temp, path)


def send_daily_once(notifier: "Telegram", state: dict[str, Any], symbol: str,
                    alert_kind: str, text: str, *, now: datetime | None = None,
                    state_path: Path | None = None) -> bool:
    """Send one alert kind per symbol per IST calendar day, persisted in C state."""
    now = now or datetime.now(IST)
    now = now.replace(tzinfo=IST) if now.tzinfo is None else now.astimezone(IST)
    ledger = state.setdefault("alerts_sent", {})
    key = f"{symbol.upper()}|{now.date().isoformat()}|{alert_kind}"
    if key in ledger:
        log.info("duplicate suppressed: %s", key)
        return False
    notifier.send(text)
    ledger[key] = now.isoformat(timespec="seconds")
    if state_path is not None:
        save_state(state_path, state)
    return True


class Telegram:
    def __init__(self, token: str, chat_id: str, dry_run: bool = False):
        self.token = token.strip()
        self.chat_id = chat_id.strip()
        self.dry_run = dry_run

    def send(self, text: str) -> bool:
        if self.dry_run:
            print("\n--- TELEGRAM DRY RUN ---\n" + text + "\n--- END ---\n")
            return True
        if not self.token or not self.chat_id:
            raise RuntimeError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID, or use --dry-run")
        import requests
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        response = requests.post(url, data={
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }, timeout=20)
        response.raise_for_status()
        body = response.json()
        if not body.get("ok"):
            raise RuntimeError(f"Telegram rejected the message: {body}")
        return True


def _fmt(value: Any) -> str:
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return "—"


def trade_plan_text(record: dict[str, Any]) -> str:
    """A complete, explicitly provisional plan block for every C trade alert."""
    entry = _finite_float(record.get("entry_estimate"))
    stop = _finite_float(record.get("stop", record.get("level")))
    target = _finite_float(record.get("target_estimate"))
    risk_per_share = max(0.0, entry - stop)
    reward_per_share = target - entry
    risk_distance_pct = risk_per_share / entry * 100.0 if entry > 0 else 0.0
    rr = reward_per_share / risk_per_share if risk_per_share > 0 and reward_per_share > 0 else None
    time_stop = int(record.get("time_stop_sessions", TIME_STOP_SESSIONS))

    capital = _finite_float(record.get("account_capital_inr", os.getenv("ACCOUNT_CAPITAL_INR", "0")))
    risk_budget_pct = _finite_float(record.get("risk_per_trade_pct", os.getenv("RISK_PER_TRADE_PCT", "1.0")), 1.0)
    if capital > 0 and risk_per_share > 0 and entry > 0:
        risk_budget = capital * risk_budget_pct / 100.0
        by_risk = math.floor(risk_budget / risk_per_share)
        by_capital = math.floor(capital / entry)
        shares = max(0, min(by_risk, by_capital))
        size_line = (f"Position-size estimate: <b>{shares}</b> shares "
                     f"(capital ₹{_fmt(capital)}, risk budget ₹{_fmt(risk_budget)} "
                     f"at {risk_budget_pct:.2f}%; gap risk may exceed this).")
    else:
        size_line = ("Position size: not calculated; set ACCOUNT_CAPITAL_INR "
                     "locally to enable a risk-budget estimate.")

    rr_text = f"{rr:.2f}R" if rr is not None else "not positive/available"
    reward_text = (f"₹{_fmt(reward_per_share)}/share" if reward_per_share > 0
                   else "no positive target distance at the alert price")
    return (
        "<b>Trade plan (C is provisional):</b>\n"
        f"• Entry estimate: <b>₹{_fmt(entry)}</b> (yfinance last price; actual fill may differ)\n"
        f"• Stop: <b>₹{_fmt(stop)}</b> (26W level)\n"
        f"• Target: <b>₹{_fmt(target)}</b> (provisional breakout-to-candidate high)\n"
        f"• Risk: ₹{_fmt(risk_per_share)}/share ({risk_distance_pct:.2f}% of entry)\n"
        f"• Reward: {reward_text} · estimated R:R: <b>{rr_text}</b>\n"
        f"• Time stop: {time_stop} trading sessions from {html.escape(str(record.get('candidate_session', 'candidate date')))}\n"
        f"• {size_line}\n"
        "No order is placed; stop/target prices are reference levels, not monitored broker orders."
    )


def candidate_message(record: dict[str, Any]) -> str:
    sym = html.escape(str(record["symbol"]))
    stamp = html.escape(str(record.get("data_as_of", "unknown")))
    settled = record.get("candle_state") == "settled_close"
    if settled:
        candle_line = ("Candle status: <b>settled</b> — this session's final NSE daily bar "
                       "(entry reference is that close; entry would be the next session).")
    else:
        age = record.get("quote_age_seconds")
        delay = (f"; provider quote was ~{int(age / 60)} min behind the exchange"
                 if isinstance(age, (int, float)) and age > 300 else "")
        candle_line = (f"Candle status: <b>still forming</b> — data as of {stamp} IST{delay}.")
    return (
        f"🟠 <b>ENTRY C — EARLY CANDIDATE · UNCONFIRMED</b>\n"
        f"<b>{sym}</b> · NSE daily candidate {html.escape(record['candidate_session'])}\n"
        f"26W breakout: {html.escape(record['breakout_session'])} · "
        f"data as of {stamp} IST\n"
        f"{candle_line}\n\n"
        f"{trade_plan_text(record)}\n\n"
        f"<b>Extra risk:</b> this candidate may never become the first precision OB. "
        f"A qualifying displacement must select this candle as its origin within "
        f"the next {int(record['max_origin_bars'])} bars. C is speculative and "
        f"is excluded from all Entry A/B statistics."
    )


def status_message(record: dict[str, Any], status: str, reason: str) -> str:
    label = {
        "confirmed": "C CANDIDATE CONFIRMED",
        "superseded": "C CANDIDATE SUPERSEDED",
        "expired": "C CANDIDATE EXPIRED",
        "invalidated": "C CANDIDATE INVALIDATED",
    }.get(status, f"C CANDIDATE {status.upper()}")
    return (
        f"ℹ️ <b>{label}</b>\n"
        f"<b>{html.escape(str(record['symbol']))}</b> · "
        f"candidate {html.escape(str(record['candidate_session']))}\n"
        f"{html.escape(reason)}\n\n"
        f"{trade_plan_text(record)}\n\n"
        f"A/B entries and statistics were not changed."
    )


def _sessions_after(bars: list[Bar], session: str) -> int:
    return sum(1 for b in bars if str(b.time)[:10] > session[:10])


def _event_for_record(bars: list[Bar], record: dict[str, Any], params: OBParams):
    cycle = BreakoutCycle(
        session=str(record["breakout_session"]),
        level=float(record["level"]), close=float(record.get("breakout_close", 0.0)),
        week_index=int(record.get("week_index", -1)))
    return first_ob_after_breakout(bars, cycle, params)


def update_pending(symbol: str, closed_bars: list[Bar], state: dict[str, Any],
                   params: OBParams, notifier: Telegram, *,
                   now: datetime | None = None,
                   state_path: Path | None = None) -> bool:
    changed = False
    for key, record in list(state["candidates"].items()):
        if record.get("symbol", "").upper() != symbol.upper() or record.get("status") != "pending":
            continue
        cycle = BreakoutCycle(
            session=record["breakout_session"], level=float(record["level"]),
            close=float(record.get("breakout_close", 0.0)),
            week_index=int(record.get("week_index", -1)))
        final_valid = _bar_final_candidate(closed_bars, record["candidate_session"], cycle, params)
        age = _sessions_after(closed_bars, record["candidate_session"])
        event = _event_for_record(closed_bars, record, params)
        status, reason = candidate_resolution(
            record["candidate_session"], record["breakout_session"], event, age,
            params.origin_search, final_candidate_valid=(final_valid is not False))
        if status != "pending":
            record["status"] = status
            record["status_reason"] = reason
            record["updated_at"] = datetime.now(IST).isoformat(timespec="seconds")
            try:
                delivered = send_daily_once(
                    notifier, state, symbol, f"C_{status}",
                    status_message(record, status, reason), now=now,
                    state_path=state_path)
                if not delivered:
                    record["notification"] = "suppressed_duplicate_same_day"
            except Exception as exc:  # keep the state retryable on Telegram error
                record["status"] = "pending"
                record.pop("status_reason", None)
                log.warning("Telegram status send failed for %s: %s", key, exc)
                raise
            changed = True
    return changed


def _is_market_time(now: datetime) -> bool:
    return now.weekday() < 5 and NSE_OPEN <= now.timetz().replace(tzinfo=None) < NSE_CLOSE


def _is_post_close_time(now: datetime, post_close_until: time | None) -> bool:
    """Weekday window after the NSE close in which today's candle is settled."""
    if post_close_until is None:
        return False
    current = now.timetz().replace(tzinfo=None)
    return now.weekday() < 5 and NSE_CLOSE <= current < post_close_until


def post_close_until_from_env() -> time | None:
    """Parse C_POST_CLOSE_UNTIL (HH:MM IST); disable with 0/off/none/disabled."""
    raw = os.getenv("C_POST_CLOSE_UNTIL", "").strip()
    if raw.lower() in {"0", "off", "none", "no", "disabled", "false"}:
        return None
    return _time_from_env("C_POST_CLOSE_UNTIL", DEFAULT_POST_CLOSE_UNTIL)


def seconds_until_next_open(now: datetime) -> int:
    """Seconds until the next weekday NSE open (holidays are not modelled)."""
    now = now.astimezone(IST) if now.tzinfo else now.replace(tzinfo=IST)
    day = now.date()
    current_time = now.timetz().replace(tzinfo=None)
    if day.weekday() < 5 and current_time < NSE_OPEN:
        target_day = day
    else:
        target_day = day + timedelta(days=1)
        while target_day.weekday() >= 5:
            target_day += timedelta(days=1)
    target = datetime.combine(target_day, NSE_OPEN, tzinfo=IST)
    return max(1, int((target - now).total_seconds()))


def _time_from_env(name: str, default: time) -> time:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        hour, minute = (int(part) for part in raw.split(":", 1))
        return time(hour, minute)
    except (ValueError, TypeError):
        raise ValueError(f"{name} must be HH:MM, got {raw!r}")


def yahoo_ticker(symbol: str) -> str:
    symbol = symbol.strip().upper()
    if symbol.endswith((".NS", ".BO")):
        return symbol
    return f"{symbol}.NS"


def _split_download(raw: Any, ticker: str, ticker_count: int):
    if raw is None or getattr(raw, "empty", True):
        return None
    columns = getattr(raw, "columns", None)
    if getattr(columns, "nlevels", 1) > 1:
        try:
            if ticker in columns.get_level_values(0):
                return raw[ticker]
            if ticker in columns.get_level_values(1):
                return raw.xs(ticker, axis=1, level=1)
        except (KeyError, IndexError, TypeError):
            return None
        return None
    return raw if ticker_count == 1 else None


def _yf_batch(yf: Any, tickers: list[str], period: str, interval: str) -> dict[str, Any]:
    raw = yf.download(
        tickers=tickers if len(tickers) > 1 else tickers[0],
        period=period, interval=interval,
        auto_adjust=False, actions=False, repair=False,
        group_by="ticker", progress=False,
        # yfinance's sqlite timezone cache is not thread safe; concurrent
        # workers produce OperationalError('database is locked') for whole
        # symbols, which silently removed them from a one-shot scan.
        threads=False,
        prepost=False, timeout=20,
    )
    return {ticker: frame for ticker in tickers
            if (frame := _split_download(raw, ticker, len(tickers))) is not None
            and not frame.empty}


def _download(tickers: list[str], period: str, interval: str) -> dict[str, Any]:
    """Batch Yahoo/yfinance download; callers tolerate missing/rate-limited rows.

    A transient failure used to drop a symbol for the whole one-shot Actions
    scan, so symbols missing from the batch are retried once as a group and
    then individually (bounded by INDIVIDUAL_RETRY_LIMIT).
    """
    if not tickers:
        return {}
    import yfinance as yf
    frames = _yf_batch(yf, tickers, period, interval)
    missing = [ticker for ticker in tickers if ticker not in frames]
    if missing:
        log.warning("yfinance returned no %s data for %s; retrying", interval,
                    ",".join(missing))
        try:
            frames.update(_yf_batch(yf, missing, period, interval))
        except Exception as exc:
            log.warning("batch retry for %s failed: %s", ",".join(missing), exc)
        missing = [ticker for ticker in tickers if ticker not in frames]
        if 0 < len(missing) <= INDIVIDUAL_RETRY_LIMIT:
            for ticker in missing:
                try:
                    frames.update(_yf_batch(yf, [ticker], period, interval))
                except Exception as exc:
                    log.warning("%s: individual retry failed: %s", ticker, exc)
    return frames


def _cached_daily_frames(tickers: list[str], session_day: date) -> dict[str, Any]:
    """Cache daily history per NSE session; retry symbols with no data."""
    global _DAILY_CACHE_DAY, _DAILY_CACHE
    if _DAILY_CACHE_DAY != session_day:
        _DAILY_CACHE_DAY = session_day
        _DAILY_CACHE = {}
    missing = [ticker for ticker in tickers if ticker not in _DAILY_CACHE]
    if missing:
        _DAILY_CACHE.update(_download(missing, DEFAULT_LOOKBACK, "1d"))
    return {ticker: _DAILY_CACHE[ticker]
            for ticker in tickers if ticker in _DAILY_CACHE}


def _index_time(value: Any) -> datetime:
    """Convert a pandas/yfinance timestamp to Asia/Kolkata."""
    if isinstance(value, datetime):
        dt = value
    else:
        # pandas.Timestamp has to_pydatetime(); avoid importing pandas in tests.
        dt = value.to_pydatetime() if hasattr(value, "to_pydatetime") else datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def daily_bars_from_frame(frame: Any, before: date | None = None) -> list[Bar]:
    bars: list[Bar] = []
    if frame is None or getattr(frame, "empty", True):
        return bars
    cols = {str(c).lower(): c for c in frame.columns}
    needed = ("open", "high", "low", "close", "volume")
    if not all(name in cols for name in needed):
        return bars
    for idx, row in frame.iterrows():
        try:
            dt = _index_time(idx)
            day = dt.date()
            if before is not None and day >= before:
                continue
            vals = [_finite_float(row[cols[name]], float("nan")) for name in needed]
            if not all(math.isfinite(v) for v in vals):
                continue
            o, h, lo, c, v = vals
            if h <= 0 or c <= 0 or v <= 0:
                # Match the shared OB port: explicit no-trade sessions are not
                # chart bars and must not inflate ATR/volume/origin-bar counts.
                continue
            bars.append(Bar(open=o, high=h, low=lo, close=c,
                            volume=v, time=day.isoformat()))
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    bars.sort(key=lambda b: str(b.time))
    return bars


def intraday_snapshot(frame: Any, today: date, now: datetime) -> IntradaySnapshot | None:
    if frame is None or getattr(frame, "empty", True):
        return None
    cols = {str(c).lower(): c for c in frame.columns}
    needed = ("open", "high", "low", "close", "volume")
    if not all(name in cols for name in needed):
        return None
    rows: list[tuple[datetime, float, float, float, float, float]] = []
    for idx, row in frame.iterrows():
        try:
            dt = _index_time(idx)
            if (dt.date() != today or dt > now + timedelta(minutes=1)
                    or dt.timetz().replace(tzinfo=None) < NSE_OPEN
                    or dt.timetz().replace(tzinfo=None) >= NSE_CLOSE):
                continue
            vals = [_finite_float(row[cols[name]], float("nan")) for name in needed]
            if not all(math.isfinite(v) for v in vals):
                continue
            o, h, lo, c, v = vals
            if c <= 0 or h <= 0:
                continue
            rows.append((dt, o, h, lo, c, max(v, 0.0)))
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    if not rows:
        return None
    rows.sort(key=lambda r: r[0])
    first, last = rows[0], rows[-1]
    live_bar = Bar(open=first[1], high=max(r[2] for r in rows),
                   low=min(r[3] for r in rows), close=last[4],
                   volume=sum(r[5] for r in rows), time=today.isoformat())
    return IntradaySnapshot(live_bar, last[0])


def quote_age_limit(max_age_seconds: int | None = None) -> int:
    """Effective MAX_QUOTE_AGE_SECONDS (seconds) used for reporting/tests."""
    if max_age_seconds:
        return int(max_age_seconds)
    raw = os.getenv("MAX_QUOTE_AGE_SECONDS", "").strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_QUOTE_AGE_SECONDS
    return value if value > 0 else DEFAULT_MAX_QUOTE_AGE_SECONDS


def quote_status(snapshot: IntradaySnapshot, now: datetime,
                 max_age_seconds: int | None = None) -> tuple[str, float]:
    """Classify a quote: (status, age_seconds).

    * ``fresh``         - inside the configured age limit.
    * ``delayed``       - older than the limit but still today's session and
                          inside HARD_QUOTE_STALE_SECONDS. Usable for a
                          *provisional* C alert; the delay is disclosed.
    * ``too-old``       - today's session but so old it cannot be trusted.
    * ``other-session`` - not from today at all (a frozen or stale feed).
    """
    age = (now - snapshot.as_of).total_seconds()
    if snapshot.as_of.date() != now.date():
        return "other-session", age
    if age > HARD_QUOTE_STALE_SECONDS:
        return "too-old", age
    if age > quote_age_limit(max_age_seconds) or age < -60:
        return "delayed", age
    return "fresh", age


def _is_fresh(snapshot: IntradaySnapshot, now: datetime,
              max_age_seconds: int | None = None) -> bool:
    """Whether a quote is inside the configured age limit (reporting helper)."""
    status, _ = quote_status(snapshot, now, max_age_seconds)
    return status == "fresh"


def check_yfinance(symbols: list[str], now: datetime | None = None) -> int:
    """Non-alerting preflight for Yahoo daily/intraday availability."""
    now = now or datetime.now(IST)
    tickers_by_symbol = {s.upper(): yahoo_ticker(s) for s in symbols}
    tickers = list(dict.fromkeys(tickers_by_symbol.values()))
    try:
        daily = _download(tickers, "5d", "1d")
        intraday = _download(tickers, "1d", "1m")
    except Exception as exc:
        print(f"yfinance request failed: {exc}")
        return 1
    daily_ok = 0
    for symbol, ticker in tickers_by_symbol.items():
        bars = daily_bars_from_frame(daily.get(ticker))
        if bars:
            daily_ok += 1
            latest = bars[-1].session
            print(f"{symbol}: daily OK; latest session {latest}")
        else:
            print(f"{symbol}: daily data unavailable")
        snap = intraday_snapshot(intraday.get(ticker), now.date(), now)
        if snap is None:
            print(f"{symbol}: no current-session 1m snapshot (market closed, stale, or unavailable)")
        else:
            age = int((now - snap.as_of).total_seconds())
            freshness = "fresh" if _is_fresh(snap, now) else "STALE"
            print(f"{symbol}: 1m snapshot {snap.as_of.isoformat(timespec='seconds')} "
                  f"({age}s old; {freshness}; last={snap.bar.close:.2f})")
    return 0 if daily_ok else 1


def _send_new_candidate(symbol: str, bars: list[Bar], candle: Bar,
                        data_as_of: datetime, settled: bool,
                        breakout: BreakoutCycle, state: dict[str, Any],
                        params: OBParams, notifier: Telegram, *,
                        now: datetime | None = None,
                        state_path: Path | None = None) -> str:
    """Store and alert one new C candidate.

    Returns ``sent``/``suppressed`` when a candidate record was stored (durable
    state changed), else ``tracked``/``not_qualifying``.
    """
    candidate_day = str(candle.time)[:10]
    key = candidate_id(symbol, breakout.session, candidate_day)
    if key in state["candidates"]:
        log.info("%s: candidate already tracked for this session", symbol)
        return "tracked"
    if not is_potential_origin(candle, breakout, params):
        log.info("%s: candle is not a qualifying bearish/neutral origin above the 26W level "
                 "(O=%.2f H=%.2f L=%.2f C=%.2f, level=%.2f)",
                 symbol, candle.open, candle.high, candle.low, candle.close, breakout.level)
        return "not_qualifying"

    target = target_for_candidate(bars, breakout.session, candidate_day,
                                  candle.high)
    now = now or datetime.now(IST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=IST)
    else:
        now = now.astimezone(IST)
    risk_pct = ((candle.close - breakout.level) / candle.close * 100.0
                if candle.close > breakout.level else None)
    record = {
        "id": key, "kind": "entry_c_candidate", "symbol": symbol.upper(),
        "breakout_session": breakout.session,
        "breakout_close": breakout.close,
        "week_index": breakout.week_index,
        "level": breakout.level,
        "candidate_session": candidate_day,
        "entry_estimate": float(candle.close),
        "entry_estimate_source": ("NSE session close (settled daily bar)" if settled
                                  else "yfinance intraday last price"),
        "candle_state": "settled_close" if settled else "forming",
        "target_estimate": target,
        "stop": breakout.level,
        "risk_distance_pct": risk_pct,
        "time_stop_sessions": TIME_STOP_SESSIONS,
        "account_capital_inr": _finite_float(os.getenv("ACCOUNT_CAPITAL_INR", "0")),
        "risk_per_trade_pct": _finite_float(os.getenv("RISK_PER_TRADE_PCT", "1.0"), 1.0),
        "data_as_of": data_as_of.isoformat(timespec="seconds"),
        "status": "pending",
        "max_origin_bars": params.origin_search,
        "created_at": now.isoformat(timespec="seconds"),
    }
    if not settled:
        record["quote_age_seconds"] = round((now - data_as_of).total_seconds())
    # Store the plan before sending so a successful delivery is durably tied to
    # the candidate. The per-symbol/type/day ledger prevents poll duplicates.
    state["candidates"][key] = record
    try:
        delivered = send_daily_once(
            notifier, state, symbol, "C_CANDIDATE", candidate_message(record),
            now=now, state_path=state_path)
    except Exception:
        state["candidates"].pop(key, None)
        raise
    if not delivered:
        record["status"] = "suppressed_duplicate_same_day"
        record["notification"] = "another C candidate alert was already sent for this symbol today"
        log.info("C candidate stored but same-day duplicate alert suppressed: %s", key)
        return "suppressed"
    log.info("C candidate sent: %s entry≈%.2f level=%.2f candle=%s", symbol,
             candle.close, breakout.level, "settled" if settled else "forming")
    return "sent"


def _evaluate_candidate(symbol: str, closed_bars: list[Bar], candle: Bar,
                        data_as_of: datetime, settled: bool,
                        state: dict[str, Any], params: OBParams,
                        notifier: Telegram, skipped: Counter, *,
                        now: datetime, state_path: Path | None) -> bool:
    """Breakout + first-OB + candidate tests on a fully known candle."""
    combined = closed_bars + [candle]
    breakout = derive_active_26w_breakout(combined)
    if breakout is None:
        log.info("%s: no active 26-week breakout; candidate skipped", symbol)
        skipped["no_breakout"] += 1
        return False

    # If a previous closed bar already birthed the cycle's first OB, the
    # C candidate window for that cycle is over. C can only anticipate it.
    if first_ob_after_breakout(closed_bars, breakout, params) is not None:
        log.info("%s: first OB already confirmed; early C window ended", symbol)
        skipped["ob_already_born"] += 1
        return False
    outcome = _send_new_candidate(symbol, combined, candle, data_as_of, settled,
                                  breakout, state, params, notifier, now=now,
                                  state_path=state_path)
    skipped[f"candidate_{outcome}"] += 1
    return outcome in {"sent", "suppressed"}


def scan_pass(symbols: list[str], state: dict[str, Any], notifier: Telegram,
              now: datetime | None = None, candidate_start: time | None = None,
              params: OBParams | None = None,
              state_path: Path | None = None,
              post_close_until: time | None = DEFAULT_POST_CLOSE_UNTIL) -> bool:
    """One market-data pass. Returns whether durable C state changed.

    Two modes:
    * live session (09:15-15:30 IST): the forming candle is aggregated from the
      latest available intraday bars; the candidate window opens at
      ``candidate_start`` (C_SIGNAL_START, default 15:15 IST).
    * post-close (15:30 IST until ``post_close_until``, default 17:00 IST): the
      session's *settled* daily bar is evaluated, so a missed late-session slot
      or a delayed provider cannot silently lose the session. Pass
      ``post_close_until=None`` to disable.

    1-minute bars are the expensive request over a whole NSE universe, so they
    are only fetched for symbols whose *daily* bars already show a live cycle
    (an active 26-week breakout, computing the forming candle from today's
    partial daily row when Yahoo provides it, and no first OB born yet). A
    cross that only happens in the last ~15 minutes is caught by the
    settled-candle pass, which needs no intraday data at all.
    """
    now = now or datetime.now(IST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=IST)
    else:
        now = now.astimezone(IST)
    params = params or OBParams()
    candidate_start = candidate_start or _time_from_env("C_SIGNAL_START", DEFAULT_C_START)
    yahoo_by_symbol = {s.upper(): yahoo_ticker(s) for s in symbols}
    tickers = list(dict.fromkeys(yahoo_by_symbol.values()))
    market_live = _is_market_time(now)
    post_close = (not market_live) and _is_post_close_time(now, post_close_until)
    if not market_live and not post_close:
        log.info("NSE market is closed and outside the post-close window; "
                 "no market-data requests")
        return False
    in_candidate_window = (market_live
                           and now.timetz().replace(tzinfo=None) >= candidate_start)

    try:
        daily_frames = _cached_daily_frames(tickers, now.date())
    except Exception:
        log.exception("yfinance download failed")
        raise

    log.info("Scan started: %d symbols at %s; %s; candidates from %s IST",
             len(yahoo_by_symbol), now.isoformat(timespec="seconds"),
             "settled post-close check" if post_close else "live session",
             candidate_start)
    changed = False
    daily_available = 0
    usable_quotes = 0
    quote_issues: list[str] = []
    skips: Counter = Counter()
    needs_quotes: list[tuple[str, str, list[Bar]]] = []

    for symbol, ticker in yahoo_by_symbol.items():
        all_daily = daily_bars_from_frame(daily_frames.get(ticker))
        if not all_daily:
            log.warning("%s: daily data unavailable; cannot evaluate C", symbol)
            skips["daily_data_unavailable"] += 1
            continue
        daily_available += 1
        today = now.date()
        # A daily row for today can be partial; exclude it while the market is
        # open and use the intraday aggregate instead.
        closed_bars = [b for b in all_daily if str(b.time)[:10] < today.isoformat()]
        settled_today = next((b for b in all_daily
                              if str(b.time)[:10] == today.isoformat()), None)

        # After the close, today's bar is final, so pending candidates can be
        # confirmed/superseded/expired/invalidated on the same evening instead
        # of waiting for the next session.
        changed |= update_pending(symbol,
                                  closed_bars + ([settled_today] if settled_today else []),
                                  state, params, notifier, now=now,
                                  state_path=state_path)

        if post_close:
            if settled_today is None:
                log.info("%s: today's settled daily bar is not published yet; "
                         "will retry on the next poll", symbol)
                skips["settled_bar_pending"] += 1
                continue
            log.info("%s: settled candle %s (O=%.2f H=%.2f L=%.2f C=%.2f V=%.0f)",
                     symbol, settled_today.session, settled_today.open,
                     settled_today.high, settled_today.low, settled_today.close,
                     settled_today.volume)
            changed |= _evaluate_candidate(symbol, closed_bars, settled_today,
                                           now, True, state, params, notifier, skips,
                                           now=now, state_path=state_path)
            continue

        if not in_candidate_window:
            log.info("%s: before C candidate window (%s IST); pending updates checked",
                     symbol, candidate_start)
            continue

        # Cheap prefilter so 1m bars are only fetched for symbols that could
        # actually qualify. Today's partial daily row carries the same
        # (provider-delayed) picture as the intraday aggregate.
        provisional = closed_bars + ([settled_today] if settled_today else [])
        breakout = derive_active_26w_breakout(provisional)
        if breakout is not None and first_ob_after_breakout(closed_bars, breakout, params) is not None:
            log.info("%s: first OB already confirmed; early C window ended", symbol)
            skips["ob_already_born"] += 1
            continue
        if breakout is None and settled_today is not None:
            log.info("%s: no active 26-week breakout; candidate skipped", symbol)
            skips["no_breakout"] += 1
            continue
        if breakout is None:
            log.info("%s: today's daily row is not published yet; checking intraday", symbol)
        needs_quotes.append((symbol, ticker, closed_bars))

    if needs_quotes:
        try:
            intraday_frames = _download([ticker for _, ticker, _ in needs_quotes],
                                        "1d", "1m")
        except Exception:
            # Degrade to "no quotes": the per-symbol loop raises a loud,
            # specific error when nothing is usable inside the window.
            log.exception("intraday download failed; continuing with no quotes")
            intraday_frames = {}
    else:
        intraday_frames = {}

    for symbol, ticker, closed_bars in needs_quotes:
        today = now.date()
        snapshot = intraday_snapshot(intraday_frames.get(ticker), today, now)
        if snapshot is None:
            log.warning("%s: no yfinance intraday bars for today's session; "
                        "C candidate skipped", symbol)
            quote_issues.append(f"{symbol}: no same-session bar")
            skips["no_intraday_bars"] += 1
            continue
        status, age = quote_status(snapshot, now)
        bar_time = snapshot.as_of.isoformat(timespec="seconds")
        if status in {"too-old", "other-session"}:
            log.warning("%s: intraday quote unusable (%s; newest bar %s, %.0fs old, "
                        "hard limit %ds); C candidate skipped",
                        symbol, status, bar_time, age, HARD_QUOTE_STALE_SECONDS)
            quote_issues.append(f"{symbol}: {age:.0f}s/{status}")
            skips[f"quote_{status}"] += 1
            continue
        if status == "delayed":
            log.warning("%s: quote delayed by the provider (newest bar %s, %.0fs old; "
                        "limit %ds). Yahoo's NSE feed normally lags ~15 min, so the "
                        "delayed bar is used and the alert states its timestamp.",
                        symbol, bar_time, age, quote_age_limit())
            skips["quote_delayed"] += 1
        else:
            log.info("%s: quote fresh (newest bar %s, %.0fs old)",
                     symbol, bar_time, age)
        usable_quotes += 1
        changed |= _evaluate_candidate(symbol, closed_bars, snapshot.bar,
                                       snapshot.as_of, False, state, params, notifier,
                                       skips, now=now, state_path=state_path)

    if not daily_available:
        raise RuntimeError("No daily data available for any configured symbol")
    if in_candidate_window and usable_quotes == 0 and quote_issues:
        raise RuntimeError(
            "No fresh intraday quotes available during the C candidate window ("
            + "; ".join(quote_issues)
            + f"; hard limit {HARD_QUOTE_STALE_SECONDS}s)")
    log.info("Scan complete: %d symbols; daily_available=%d usable_quotes=%d "
             "state_changed=%s; %s", len(yahoo_by_symbol), daily_available,
             usable_quotes, changed,
             ", ".join(f"{name}={count}" for name, count in sorted(skips.items())))
    return changed


def load_symbols(path: Path, cli_symbols: str | None = None) -> list[str]:
    if cli_symbols:
        symbols = [x.strip().upper() for x in cli_symbols.split(",") if x.strip()]
    else:
        symbols = []
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.split("#", 1)[0].strip()
                if line:
                    symbols.append(line.upper())
    # Preserve order while de-duplicating.
    return list(dict.fromkeys(symbols))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Early Entry C candidate alerts; A/B are not modified.")
    ap.add_argument("--symbols", help="comma-separated NSE symbols, e.g. RELIANCE,TCS")
    ap.add_argument("--symbols-file", default=str(ROOT / "symbols.txt"))
    ap.add_argument("--state-file", default=str(DEFAULT_STATE_FILE))
    ap.add_argument("--poll-seconds", type=int, default=DEFAULT_POLL_SECONDS)
    ap.add_argument("--once", action="store_true", help="run one pass and exit")
    ap.add_argument("--dry-run", action="store_true", help="print Telegram messages; do not send or save state")
    ap.add_argument("--check-yfinance", action="store_true", help="check Yahoo daily/intraday data; do not alert")
    ap.add_argument("--test-telegram", action="store_true", help="send one Telegram test message, then exit")
    args = ap.parse_args(argv)

    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    symbols = load_symbols(Path(args.symbols_file), args.symbols)
    if not symbols:
        log.error("No symbols configured. Add NSE symbols to symbols.txt or pass --symbols.")
        return 2
    if args.check_yfinance:
        return check_yfinance(symbols)
    dry_run = args.dry_run or os.getenv("DRY_RUN", "0").strip().lower() in {"1", "true", "yes"}
    notifier = Telegram(os.getenv("TELEGRAM_BOT_TOKEN", ""),
                        os.getenv("TELEGRAM_CHAT_ID", ""), dry_run=dry_run)
    if not dry_run and (not notifier.token or not notifier.chat_id):
        log.error("Telegram credentials missing. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID, or use --dry-run.")
        return 2
    if args.test_telegram:
        notifier.send("✅ <b>Entry C Telegram connection test</b>\nNo market signal or order was generated.")
        return 0

    state_path = Path(args.state_file)
    try:
        state = load_state(state_path)
    except RuntimeError as exc:
        log.error("%s", exc)
        return 2
    if not dry_run:
        # Persist even an empty first-run ledger so Actions can restore it.
        save_state(state_path, state)
    poll_seconds = max(15, int(args.poll_seconds))
    try:
        candidate_start = _time_from_env("C_SIGNAL_START", DEFAULT_C_START)
        post_close_until = post_close_until_from_env()
    except ValueError as exc:
        log.error("%s", exc)
        return 2
    log.info("Scanner configured: symbols=%s dry_run=%s state=%s candidate_start=%s IST "
             "post_close_until=%s quote_age_limit=%ds",
             ",".join(symbols), dry_run, state_path, candidate_start,
             post_close_until or "disabled", quote_age_limit())
    while True:
        current = datetime.now(IST)
        if not args.once and not (_is_market_time(current)
                                  or _is_post_close_time(current, post_close_until)):
            wait = seconds_until_next_open(current)
            next_open = (current + timedelta(seconds=wait)).strftime("%Y-%m-%d %H:%M")
            log.info("NSE closed; next weekday scan at %s IST", next_open)
            time_module.sleep(wait)
            continue
        try:
            changed = scan_pass(symbols, state, notifier, now=current,
                               candidate_start=candidate_start,
                               post_close_until=post_close_until,
                               state_path=None if dry_run else state_path)
            if changed and not dry_run:
                save_state(state_path, state)
        except KeyboardInterrupt:
            log.info("Stopped by user")
            return 0
        except Exception:
            log.exception("Entry C scan pass failed")
            if args.once:
                return 1
        if args.once:
            return 0
        time_module.sleep(poll_seconds)


if __name__ == "__main__":
    sys.exit(main())
