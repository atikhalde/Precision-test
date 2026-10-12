import unittest
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
    Telegram,
    candidate_id,
    candidate_message,
    candidate_resolution,
    derive_active_26w_breakout,
    is_potential_origin,
    load_state,
    scan_pass,
    seconds_until_next_open,
    send_daily_once,
    status_message,
    target_for_candidate,
)
from ob_precision import Bar, OBParams, replay


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


if __name__ == "__main__":
    unittest.main()
