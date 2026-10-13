import unittest
import json
import os
from contextlib import redirect_stdout
from datetime import date, datetime, time as dt_time, timedelta
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

from entry_c import (
    BreakoutCycle,
    IntradaySnapshot,
    Telegram,
    _download,
    _is_post_close_time,
    _send_new_candidate,
    candidate_id,
    candidate_message,
    candidate_resolution,
    derive_active_26w_breakout,
    is_potential_origin,
    load_state,
    load_symbols,
    main,
    post_close_until_from_env,
    quote_age_limit,
    quote_status,
    scan_pass,
    seconds_until_next_open,
    send_daily_once,
    status_message,
    target_for_candidate,
    update_pending,
)
from ob_precision import Bar, OBParams, replay


IST = ZoneInfo("Asia/Kolkata")


class RecordingTelegram:
    """Stand-in notifier that records instead of sending."""

    def __init__(self):
        self.messages = []

    def send(self, text):
        self.messages.append(text)
        return True


def flat_breakout_history(today: date) -> pd.DataFrame:
    """26 weeks of flat weekday bars before `today`; the 26W level is 100."""
    first_monday = today - timedelta(weeks=26)
    rows, index = [], []
    for week in range(26):
        for weekday in range(5):
            day = first_monday + timedelta(days=week * 7 + weekday)
            if day < today:
                index.append(day)
                rows.append({"Open": 99, "High": 100, "Low": 97,
                             "Close": 99, "Volume": 1000})
    return pd.DataFrame(rows, index=pd.to_datetime(index))


def add_daily_bar(frame: pd.DataFrame, day: date, **values) -> pd.DataFrame:
    frame = frame.copy()
    frame.loc[pd.Timestamp(day)] = values
    return frame


class EntryCCandidateTests(unittest.TestCase):
    def setUp(self):
        self.cycle = BreakoutCycle("2026-01-05", 100.0, 101.0, 26)
        self.params = OBParams()

    def test_bearish_close_above_level_is_candidate(self):
        bar = Bar(105, 106, 101, 102, 1000, "2026-01-06")
        self.assertTrue(is_potential_origin(bar, self.cycle, self.params))

    def test_neutral_close_above_level_is_candidate(self):
        bar = Bar(103, 105, 101, 103.2, 1000, "2026-01-06")
        self.assertTrue(is_potential_origin(bar, self.cycle, self.params))

    def test_bullish_non_neutral_is_not_candidate(self):
        bar = Bar(101, 109, 100, 108, 1000, "2026-01-06")
        self.assertFalse(is_potential_origin(bar, self.cycle, self.params))

    def test_close_must_be_strictly_above_level(self):
        bar = Bar(105, 106, 100, 100, 1000, "2026-01-06")
        self.assertFalse(is_potential_origin(bar, self.cycle, self.params))

    def test_origin_must_be_on_or_after_breakout(self):
        bar = Bar(105, 106, 101, 102, 1000, "2026-01-04")
        self.assertFalse(is_potential_origin(bar, self.cycle, self.params))

    def test_open_to_low_zone_must_have_width(self):
        bar = Bar(101, 106, 101, 102, 1000, "2026-01-06")
        self.assertFalse(is_potential_origin(bar, self.cycle, self.params))

    def test_candidate_resolution_lifecycle(self):
        self.assertEqual(
            candidate_resolution("2026-01-06", "2026-01-05", None, 3)[0],
            "pending")
        self.assertEqual(
            candidate_resolution("2026-01-06", "2026-01-05", None, 9)[0],
            "expired")
        self.assertEqual(
            candidate_resolution("2026-01-06", "2026-01-05", None, 1,
                                 final_candidate_valid=False)[0],
            "invalidated")

    def test_candidate_confirms_only_if_selected_as_origin(self):
        matching = SimpleNamespace(zone=SimpleNamespace(
            origin_session="2026-01-06", born_session="2026-01-08"))
        other = SimpleNamespace(zone=SimpleNamespace(
            origin_session="2026-01-07", born_session="2026-01-08"))
        self.assertEqual(candidate_resolution(
            "2026-01-06", "2026-01-05", matching, 2)[0], "confirmed")
        self.assertEqual(candidate_resolution(
            "2026-01-06", "2026-01-05", other, 2)[0], "superseded")

    def test_id_is_cycle_and_session_scoped(self):
        self.assertEqual(candidate_id("tcs", "2026-01-05", "2026-01-06"),
                         "C|TCS|2026-01-05|2026-01-06")

    def test_target_window_includes_candidate_but_not_earlier_bars(self):
        bars = [
            Bar(9, 10, 8, 9, 100, "2026-01-04"),
            Bar(100, 103, 99, 102, 100, "2026-01-05"),
            Bar(102, 107, 101, 103, 100, "2026-01-06"),
        ]
        target = target_for_candidate(bars, "2026-01-05", "2026-01-06", 107)
        self.assertEqual(target, 107)

    def test_message_is_clearly_unconfirmed_and_does_not_claim_fill(self):
        record = {
            "symbol": "TCS", "candidate_session": "2026-01-06",
            "entry_estimate": 123.45, "breakout_session": "2026-01-05",
            "level": 120.0, "target_estimate": 130.0, "risk_pct": 2.8,
            "data_as_of": "2026-01-06T15:20:00+05:30", "max_origin_bars": 8,
        }
        text = candidate_message(record)
        self.assertIn("EARLY CANDIDATE", text)
        self.assertIn("UNCONFIRMED", text)
        self.assertIn("Trade plan", text)
        self.assertIn("Stop:", text)
        self.assertIn("Target:", text)
        self.assertIn("90 trading sessions", text)
        self.assertIn("actual fill may differ", text)
        self.assertIn("No order is placed", text)

    def test_telegram_dry_run_prints_without_credentials(self):
        output = StringIO()
        with redirect_stdout(output):
            sent = Telegram("", "", dry_run=True).send("test alert")
        self.assertTrue(sent)
        self.assertIn("TELEGRAM DRY RUN", output.getvalue())
        self.assertIn("test alert", output.getvalue())

    def test_all_trade_status_messages_include_the_plan(self):
        record = {
            "symbol": "TCS", "candidate_session": "2026-01-06",
            "entry_estimate": 123.45, "breakout_session": "2026-01-05",
            "level": 120.0, "stop": 120.0, "target_estimate": 130.0,
            "time_stop_sessions": 90,
        }
        for status in ("confirmed", "superseded", "expired", "invalidated"):
            message = status_message(record, status, "example reason")
            self.assertIn("Trade plan", message)
            self.assertIn("Entry estimate", message)
            self.assertIn("Stop", message)
            self.assertIn("Target", message)
            self.assertIn("90 trading sessions", message)

    def test_same_alert_kind_is_suppressed_for_symbol_on_same_ist_day(self):
        class Counter:
            def __init__(self):
                self.messages = []
            def send(self, text):
                self.messages.append(text)
                return True

        ist = ZoneInfo("Asia/Kolkata")
        notifier = Counter()
        state = {"alerts_sent": {}}
        now = datetime(2026, 10, 12, 15, 20, tzinfo=ist)
        self.assertTrue(send_daily_once(notifier, state, "TCS", "C_CANDIDATE", "plan", now=now))
        self.assertFalse(send_daily_once(notifier, state, "TCS", "C_CANDIDATE", "duplicate", now=now))
        self.assertTrue(send_daily_once(notifier, state, "TCS", "C_CONFIRMED", "status plan", now=now))
        self.assertTrue(send_daily_once(notifier, state, "TCS", "C_CANDIDATE", "next day", now=now + timedelta(days=1)))
        self.assertEqual(len(notifier.messages), 3)

    def test_daily_dedupe_ledger_persists_across_restart(self):
        class Counter:
            def __init__(self):
                self.messages = []
            def send(self, text):
                self.messages.append(text)
                return True

        notifier = Counter()
        now = datetime(2026, 10, 12, 15, 20, tzinfo=ZoneInfo("Asia/Kolkata"))
        with TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            state = {"candidates": {}, "alerts_sent": {}}
            self.assertTrue(send_daily_once(
                notifier, state, "TCS", "C_CANDIDATE", "plan",
                now=now, state_path=state_path))
            reloaded = load_state(state_path)
            self.assertFalse(send_daily_once(
                notifier, reloaded, "TCS", "C_CANDIDATE", "duplicate",
                now=now, state_path=state_path))
        self.assertEqual(len(notifier.messages), 1)

    def test_next_open_skips_weekend(self):
        ist = ZoneInfo("Asia/Kolkata")
        saturday = datetime(2026, 10, 10, 16, 0, tzinfo=ist)
        next_open = saturday + timedelta(seconds=seconds_until_next_open(saturday))
        self.assertEqual(next_open.date(), date(2026, 10, 12))
        self.assertEqual(next_open.hour, 9)
        self.assertEqual(next_open.minute, 15)


class BreakoutAndReplayTests(unittest.TestCase):
    @staticmethod
    def make_weekly_bars():
        start = date(2025, 1, 6)  # Monday
        bars = []
        for week in range(27):
            for session in range(5):
                day = start + timedelta(days=week * 7 + session)
                if week < 26:
                    bars.append(Bar(99, 100, 97, 99, 1000, day.isoformat()))
                else:
                    # The first session of the 27th week crosses the 26-week level.
                    close = 101 if session == 0 else 100.5
                    bars.append(Bar(100, 102, 99, close, 1200, day.isoformat()))
        return bars

    def test_26_week_breakout_uses_prior_week_highs_and_close_cross(self):
        bars = self.make_weekly_bars()
        cycle = derive_active_26w_breakout(bars)
        self.assertIsNotNone(cycle)
        self.assertEqual(cycle.session, (date(2025, 1, 6) + timedelta(days=26 * 7)).isoformat())
        self.assertEqual(cycle.level, 100)

    def test_replay_detects_origin_for_confirmation(self):
        start = date(2026, 1, 1)
        bars = []
        for i in range(26):
            day = start + timedelta(days=i)
            bars.append(Bar(99, 100, 98, 99, 1000, day.isoformat()))
        # Bearish, above-level origin with a non-zero open-to-low zone.
        origin_day = start + timedelta(days=26)
        bars.append(Bar(103, 105, 100, 102, 1000, origin_day.isoformat()))
        # Strong displacement: high range, RVOL, body, CLV and structure close.
        disp_day = start + timedelta(days=27)
        bars.append(Bar(102, 110, 101, 109, 5000, disp_day.isoformat()))
        result = replay(bars, OBParams())
        births = [e for e in result.events if e.kind == "ob"]
        self.assertTrue(births)
        self.assertEqual(births[-1].zone.origin_session, origin_day.isoformat())
        self.assertEqual(births[-1].zone.born_session, disp_day.isoformat())

    def test_scan_pass_sends_one_complete_c_plan_and_dedupes_poll(self):
        today = date(2026, 10, 12)  # Monday
        first_monday = today - timedelta(weeks=26)
        rows = []
        days = []
        for week in range(26):
            for weekday in range(5):
                day = first_monday + timedelta(days=week * 7 + weekday)
                days.append(day)
                rows.append({"Open": 99, "High": 100, "Low": 97,
                             "Close": 99, "Volume": 1000})
        daily = pd.DataFrame(rows, index=pd.to_datetime(days))
        ist = ZoneInfo("Asia/Kolkata")
        minute_index = pd.DatetimeIndex([
            datetime(2026, 10, 12, 9, 15, tzinfo=ist),
            datetime(2026, 10, 12, 15, 19, tzinfo=ist),
        ])
        intraday = pd.DataFrame([
            {"Open": 102, "High": 104, "Low": 101, "Close": 103, "Volume": 1000},
            {"Open": 103, "High": 105, "Low": 100.5, "Close": 101, "Volume": 500},
        ], index=minute_index)

        class Counter:
            def __init__(self):
                self.messages = []
            def send(self, text):
                self.messages.append(text)
                return True

        notifier = Counter()
        state = {"candidates": {}, "alerts_sent": {}}
        now = datetime(2026, 10, 12, 15, 20, tzinfo=ist)
        with patch("entry_c._cached_daily_frames", return_value={"TCS.NS": daily}), \
             patch("entry_c._download", return_value={"TCS.NS": intraday}):
            first_changed = scan_pass(["TCS"], state, notifier, now=now,
                                      candidate_start=dt_time(15, 15))
            second_changed = scan_pass(["TCS"], state, notifier, now=now,
                                       candidate_start=dt_time(15, 15))
        self.assertTrue(first_changed)
        self.assertFalse(second_changed)
        self.assertEqual(len(notifier.messages), 1)
        self.assertIn("ENTRY C", notifier.messages[0])
        self.assertIn("Trade plan", notifier.messages[0])
        self.assertIn("Target:", notifier.messages[0])
        self.assertIn("Time stop: 90", notifier.messages[0])



class ScannerOperationsTests(unittest.TestCase):
    def test_default_watchlist_is_not_empty(self):
        symbols = load_symbols(Path(__file__).resolve().parents[1] / "symbols.txt")
        self.assertTrue(symbols)
        self.assertEqual(len(symbols), len(set(symbols)))

    def test_corrupt_ledger_fails_closed(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text("not JSON")
            with self.assertRaisesRegex(RuntimeError, "refusing to reset"):
                load_state(path)
            self.assertEqual(path.read_text(), "not JSON")

    def test_invalid_ledger_fields_fail_closed(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            for field in ("candidates", "alerts_sent"):
                path.write_text(json.dumps({field: []}))
                with self.assertRaisesRegex(RuntimeError, "refusing to reset"):
                    load_state(path)

    def test_missing_ledger_initializes_empty(self):
        with TemporaryDirectory() as directory:
            self.assertEqual(load_state(Path(directory) / "missing.json")["alerts_sent"], {})

    def test_once_scan_exception_returns_failure(self):
        with TemporaryDirectory() as directory, patch("dotenv.load_dotenv"), \
             patch.dict(os.environ, {}, clear=True), \
             patch("entry_c.scan_pass", side_effect=RuntimeError("provider unavailable")):
            path = Path(directory) / "state.json"
            self.assertEqual(main(["--symbols", "TCS", "--once", "--dry-run",
                                   "--state-file", str(path)]), 1)
            self.assertFalse(path.exists())

    def test_live_first_run_persists_empty_ledger(self):
        with TemporaryDirectory() as directory, patch("dotenv.load_dotenv"), \
             patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test", "TELEGRAM_CHAT_ID": "test"}, clear=True), \
             patch("entry_c.scan_pass", return_value=False):
            path = Path(directory) / "state.json"
            self.assertEqual(main(["--symbols", "TCS", "--once", "--state-file", str(path)]), 0)
            self.assertEqual(load_state(path)["alerts_sent"], {})

    def test_main_does_not_overwrite_corrupt_ledger(self):
        with TemporaryDirectory() as directory, patch("dotenv.load_dotenv"), \
             patch.dict(os.environ, {}, clear=True):
            path = Path(directory) / "state.json"
            path.write_text("corrupt")
            self.assertEqual(main(["--symbols", "TCS", "--once", "--dry-run",
                                   "--state-file", str(path)]), 2)
            self.assertEqual(path.read_text(), "corrupt")

    def test_market_closed_outside_post_close_window_does_not_download(self):
        with patch("entry_c._download") as download, patch("entry_c._cached_daily_frames") as daily:
            self.assertFalse(scan_pass(["TCS"], {}, Telegram("", "", True),
                now=datetime(2026, 10, 13, 19, 0, tzinfo=ZoneInfo("Asia/Kolkata"))))
            download.assert_not_called()
            daily.assert_not_called()

    def test_post_close_window_can_be_disabled(self):
        with patch("entry_c._download") as download, patch("entry_c._cached_daily_frames") as daily:
            self.assertFalse(scan_pass(["TCS"], {}, Telegram("", "", True),
                now=datetime(2026, 10, 13, 16, 0, tzinfo=ZoneInfo("Asia/Kolkata")),
                post_close_until=None))
            download.assert_not_called()
            daily.assert_not_called()

    def test_complete_daily_data_outage_raises(self):
        with patch("entry_c._cached_daily_frames", return_value={}), \
             patch("entry_c._download", return_value={}):
            with self.assertRaisesRegex(RuntimeError, "No daily data"):
                scan_pass(["TCS"], {"candidates": {}, "alerts_sent": {}}, Telegram("", "", True),
                    now=datetime(2026, 10, 13, 15, 20, tzinfo=ZoneInfo("Asia/Kolkata")))

    def test_download_exception_propagates(self):
        with patch("entry_c._cached_daily_frames", side_effect=RuntimeError("download broke")):
            with self.assertRaisesRegex(RuntimeError, "download broke"):
                scan_pass(["TCS"], {}, Telegram("", "", True),
                    now=datetime(2026, 10, 13, 15, 20, tzinfo=ZoneInfo("Asia/Kolkata")))

    def test_no_fresh_quotes_in_window_raises(self):
        frame = pd.DataFrame([{"Open": 100, "High": 105, "Low": 99, "Close": 102, "Volume": 100}],
                             index=pd.to_datetime(["2026-10-12"]))
        with patch("entry_c._cached_daily_frames", return_value={"TCS.NS": frame}), \
             patch("entry_c._download", return_value={}):
            with self.assertRaisesRegex(RuntimeError, "No fresh intraday quotes"):
                scan_pass(["TCS"], {"candidates": {}, "alerts_sent": {}}, Telegram("", "", True),
                    now=datetime(2026, 10, 13, 15, 20, tzinfo=ZoneInfo("Asia/Kolkata")))

    def test_status_delivery_failure_stays_retryable_and_propagates(self):
        record = {"symbol": "TCS", "status": "pending", "breakout_session": "2026-10-01",
                  "candidate_session": "2026-10-12", "level": 100.0}
        state = {"candidates": {"candidate": record}, "alerts_sent": {}}
        notifier = SimpleNamespace(send=lambda text: (_ for _ in ()).throw(RuntimeError("delivery failed")))
        with patch("entry_c._event_for_record", return_value=None), \
             patch("entry_c.candidate_resolution", return_value=("confirmed", "selected")), \
             patch("entry_c.status_message", return_value="status plan"):
            with self.assertRaisesRegex(RuntimeError, "delivery failed"):
                update_pending("TCS", [], state, OBParams(), notifier)
        self.assertEqual(record["status"], "pending")
        self.assertEqual(state["alerts_sent"], {})

    def test_post_close_pass_alerts_on_settled_candle_without_intraday_request(self):
        today = date(2026, 10, 12)  # Monday
        daily = add_daily_bar(flat_breakout_history(today), today,
                              Open=102, High=105, Low=100.5, Close=101, Volume=1500)
        notifier = RecordingTelegram()
        state = {"candidates": {}, "alerts_sent": {}}
        with patch("entry_c._cached_daily_frames", return_value={"TCS.NS": daily}), \
             patch("entry_c._download") as download:
            changed = scan_pass(["TCS"], state, notifier,
                                now=datetime(2026, 10, 12, 16, 0, tzinfo=IST))
        download.assert_not_called()  # the settled daily bar needs no 1m request
        self.assertTrue(changed)
        self.assertEqual(len(notifier.messages), 1)
        self.assertIn("ENTRY C", notifier.messages[0])
        self.assertIn("settled", notifier.messages[0])
        record = next(iter(state["candidates"].values()))
        self.assertEqual(record["candle_state"], "settled_close")
        self.assertEqual(record["entry_estimate"], 101)
        self.assertEqual(record["candidate_session"], today.isoformat())

    def test_post_close_pass_does_not_repeat_an_intraday_alert(self):
        today = date(2026, 10, 12)
        history = flat_breakout_history(today)
        settled = add_daily_bar(history, today, Open=102, High=105, Low=100.5,
                                Close=101, Volume=1500)
        intraday = pd.DataFrame(
            [{"Open": 102, "High": 105, "Low": 100.5, "Close": 101, "Volume": 1500}],
            index=pd.DatetimeIndex([datetime(2026, 10, 12, 15, 19, tzinfo=IST)]))
        notifier = RecordingTelegram()
        state = {"candidates": {}, "alerts_sent": {}}
        with patch("entry_c._cached_daily_frames",
                   side_effect=[{"TCS.NS": history}, {"TCS.NS": settled}]), \
             patch("entry_c._download", return_value={"TCS.NS": intraday}):
            self.assertTrue(scan_pass(["TCS"], state, notifier,
                                      now=datetime(2026, 10, 12, 15, 20, tzinfo=IST)))
            self.assertFalse(scan_pass(["TCS"], state, notifier,
                                       now=datetime(2026, 10, 12, 16, 0, tzinfo=IST)))
        self.assertEqual(len(notifier.messages), 1)
        self.assertEqual(len(state["candidates"]), 1)

    def test_delayed_quote_is_used_and_disclosed(self):
        today = date(2026, 10, 12)
        history = flat_breakout_history(today)  # today's row is still forming
        intraday = pd.DataFrame(
            [{"Open": 102, "High": 105, "Low": 100.5, "Close": 101, "Volume": 1500}],
            index=pd.DatetimeIndex([datetime(2026, 10, 12, 14, 55, tzinfo=IST)]))
        notifier = RecordingTelegram()
        state = {"candidates": {}, "alerts_sent": {}}
        with patch.dict(os.environ, {"MAX_QUOTE_AGE_SECONDS": "300"}), \
             patch("entry_c._cached_daily_frames", return_value={"TCS.NS": history}), \
             patch("entry_c._download", return_value={"TCS.NS": intraday}), \
             self.assertLogs("entry_c", level="WARNING") as logs:
            changed = scan_pass(["TCS"], state, notifier,
                                now=datetime(2026, 10, 12, 15, 20, tzinfo=IST))
        self.assertTrue(changed)
        self.assertTrue(any("delayed by the provider" in line for line in logs.output))
        self.assertEqual(len(notifier.messages), 1)
        record = next(iter(state["candidates"].values()))
        self.assertEqual(record["candle_state"], "forming")
        self.assertAlmostEqual(record["quote_age_seconds"], 1500, delta=2)

    def test_quote_from_a_previous_session_is_never_used(self):
        today = date(2026, 10, 12)
        intraday = pd.DataFrame(
            [{"Open": 102, "High": 105, "Low": 100.5, "Close": 101, "Volume": 1500}],
            index=pd.DatetimeIndex([datetime(2026, 10, 9, 15, 20, tzinfo=IST)]))
        with patch.dict(os.environ, {"MAX_QUOTE_AGE_SECONDS": "300"}), \
             patch("entry_c._cached_daily_frames",
                   return_value={"TCS.NS": flat_breakout_history(today)}), \
             patch("entry_c._download", return_value={"TCS.NS": intraday}):
            with self.assertRaisesRegex(RuntimeError, "No fresh intraday quotes"):
                scan_pass(["TCS"], {"candidates": {}, "alerts_sent": {}},
                          RecordingTelegram(),
                          now=datetime(2026, 10, 12, 15, 20, tzinfo=IST))

    def test_pre_window_skip_is_logged(self):
        frame = pd.DataFrame([{"Open": 100, "High": 105, "Low": 99, "Close": 102, "Volume": 100}],
                             index=pd.to_datetime(["2026-10-12"]))
        with patch("entry_c._cached_daily_frames", return_value={"TCS.NS": frame}), \
             patch("entry_c._download", return_value={}), self.assertLogs("entry_c", level="INFO") as logs:
            self.assertFalse(scan_pass(["TCS"], {"candidates": {}, "alerts_sent": {}}, Telegram("", "", True),
                now=datetime(2026, 10, 13, 14, 0, tzinfo=ZoneInfo("Asia/Kolkata"))))
        self.assertTrue(any("before C candidate window" in line for line in logs.output))

class QuoteStatusTests(unittest.TestCase):
    @staticmethod
    def snapshot(when: datetime) -> IntradaySnapshot:
        return IntradaySnapshot(Bar(1, 2, 0.5, 1.5, 10, when.date().isoformat()), when)

    def test_quote_age_classification(self):
        now = datetime(2026, 10, 13, 15, 20, tzinfo=IST)
        cases = {
            "fresh": datetime(2026, 10, 13, 15, 18, tzinfo=IST),
            "delayed": datetime(2026, 10, 13, 15, 0, tzinfo=IST),
            "too-old": datetime(2026, 10, 13, 13, 0, tzinfo=IST),
            "other-session": datetime(2026, 10, 10, 15, 0, tzinfo=IST),
        }
        for expected, when in cases.items():
            with self.subTest(expected=expected):
                self.assertEqual(quote_status(self.snapshot(when), now, 300)[0], expected)

    def test_quote_age_limit_defaults_match_the_provider_delay(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(quote_age_limit(), 1200)
        with patch.dict(os.environ, {"MAX_QUOTE_AGE_SECONDS": "300"}):
            self.assertEqual(quote_age_limit(), 300)
        with patch.dict(os.environ, {"MAX_QUOTE_AGE_SECONDS": "not-a-number"}):
            self.assertEqual(quote_age_limit(), 1200)
        with patch.dict(os.environ, {"MAX_QUOTE_AGE_SECONDS": "0"}):
            self.assertEqual(quote_age_limit(), 1200)

    def test_post_close_window_helpers(self):
        self.assertTrue(_is_post_close_time(
            datetime(2026, 10, 13, 15, 45, tzinfo=IST), dt_time(17, 0)))
        self.assertFalse(_is_post_close_time(
            datetime(2026, 10, 13, 15, 29, tzinfo=IST), dt_time(17, 0)))
        self.assertFalse(_is_post_close_time(
            datetime(2026, 10, 13, 16, 0, tzinfo=IST), None))
        self.assertFalse(_is_post_close_time(  # Saturday
            datetime(2026, 10, 10, 16, 0, tzinfo=IST), dt_time(17, 0)))

    def test_post_close_until_env_parsing(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(post_close_until_from_env(), dt_time(17, 0))
        with patch.dict(os.environ, {"C_POST_CLOSE_UNTIL": "16:45"}):
            self.assertEqual(post_close_until_from_env(), dt_time(16, 45))
        for token in ("0", "off", "none", "disabled"):
            with patch.dict(os.environ, {"C_POST_CLOSE_UNTIL": token}):
                self.assertIsNone(post_close_until_from_env())
        with patch.dict(os.environ, {"C_POST_CLOSE_UNTIL": "nonsense"}):
            with self.assertRaises(ValueError):
                post_close_until_from_env()


class DownloadRetryTests(unittest.TestCase):
    @staticmethod
    def frame() -> pd.DataFrame:
        return pd.DataFrame([{"Open": 1, "High": 2, "Low": 0.5, "Close": 1.5,
                              "Volume": 10}], index=pd.to_datetime(["2026-10-12"]))

    def test_symbol_dropped_by_the_batch_is_retried_before_giving_up(self):
        calls = []

        def fake_batch(_yf, tickers, _period, _interval):
            calls.append(list(tickers))
            if "TCS.NS" in tickers and len(calls) > 1:
                return {"TCS.NS": self.frame()}
            return {}

        with patch("entry_c._yf_batch", side_effect=fake_batch):
            frames = _download(["TCS.NS", "INFY.NS"], "2y", "1d")
        self.assertIn("TCS.NS", frames)
        self.assertNotIn("INFY.NS", frames)
        self.assertEqual(calls, [["TCS.NS", "INFY.NS"], ["TCS.NS", "INFY.NS"],
                                 ["INFY.NS"]])

    def test_successful_batch_is_not_retried(self):
        with patch("entry_c._yf_batch", return_value={"TCS.NS": self.frame()}) as batch:
            frames = _download(["TCS.NS"], "2y", "1d")
        self.assertIn("TCS.NS", frames)
        batch.assert_called_once()


class CandidateContractTests(unittest.TestCase):
    def test_non_qualifying_candle_reports_status_and_leaves_state_untouched(self):
        cycle = BreakoutCycle("2026-01-05", 100.0, 101.0, 26)
        candle = Bar(101, 109, 100, 108, 1000, "2026-01-06")  # bullish, not neutral
        state = {"candidates": {}, "alerts_sent": {}}
        status = _send_new_candidate(
            "TCS", [candle], candle, datetime(2026, 1, 6, 15, 20, tzinfo=IST),
            False, cycle, state, OBParams(), Telegram("", "", True))
        self.assertEqual(status, "not_qualifying")
        self.assertEqual(state["candidates"], {})
        self.assertEqual(state["alerts_sent"], {})

    def test_candidate_already_tracked_is_reported_without_a_second_alert(self):
        cycle = BreakoutCycle("2026-01-05", 100.0, 101.0, 26)
        candle = Bar(105, 106, 101, 102, 1000, "2026-01-06")
        state = {"candidates": {candidate_id("TCS", cycle.session, "2026-01-06"): {}},
                 "alerts_sent": {}}
        notifier = RecordingTelegram()
        status = _send_new_candidate(
            "TCS", [candle], candle, datetime(2026, 1, 6, 15, 20, tzinfo=IST),
            False, cycle, state, OBParams(), notifier)
        self.assertEqual(status, "tracked")
        self.assertEqual(notifier.messages, [])


if __name__ == "__main__":
    unittest.main()
