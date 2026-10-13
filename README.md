# Precision-test — Entry C early-candidate scanner

This repository contains an **Entry C-only add-on** for the Precision OB setup. It does not implement, replace, or edit Entry A or Entry B. The GitHub repository was empty apart from its placeholder README when inspected; no A/B source files were present to modify. Keep your existing A/B project separate and unchanged.

## What Entry C means

Entry C tries to reach an A-like price **before** the displacement candle confirms the order block. It is deliberately speculative:

1. During the configured late-session window, the scanner looks for a forming NSE daily candle that is bearish or neutral under the OB settings, closes above the active 26-week level, and has a non-zero open-to-low zone.
2. It sends `ENTRY C — EARLY CANDIDATE · UNCONFIRMED`. The C entry shown is the latest yfinance intraday price at alert time—not a guaranteed fill and not necessarily the final daily close.
3. The candidate remains pending for up to eight sessions. The vendored `ob_precision.replay()` engine checks whether a later qualifying displacement selects that exact candle as the origin of the cycle's first post-breakout OB.
4. On a later market-hours poll, once settled daily bars are available, the scanner sends a separate confirmation, superseded, expired, or invalidated update. Each C candidate has its own record. A future confirmation does **not** change the historical fact that C was entered speculatively.

Every C entry and follow-up alert includes the full provisional trade plan: estimated entry, 26W stop, target, risk per share/percent, estimated reward:risk, 90-session time stop, and—if you configure account capital—a position-size estimate. **The same symbol + alert type is deduplicated for each IST calendar day**, with a persistent ledger so restarting the scanner does not resend the same alert. A genuinely different status transition is a distinct alert and also includes the plan. Telegram does not provide an idempotency key, so an ambiguous network timeout after Telegram accepts a message cannot be made mathematically exactly-once by a client; the persisted ledger prevents ordinary polling/restart duplicates.

C alerts include a provisional 26W stop and a provisional target (the highest high from breakout through the candidate session). **C outcomes must be measured separately.** False candidates, gaps, actual fill prices, and any exits before confirmation belong in the C backtest; do not add them to A/B statistics. The scanner sends alerts only—it does not place orders or monitor broker exits; the displayed stop/target are reference levels.

### Important timing limitation

An end-of-day alert cannot guarantee execution at the just-printed close. The default alert window begins at **15:15 Asia/Kolkata** so a candidate can be noticed before the NSE close; its candle shape and price are still provisional. Earlier alerts are possible by setting `C_SIGNAL_START` earlier, but increase false candidates.

**Yahoo's NSE intraday feed normally lags the exchange by about 15 minutes.** A quote is therefore never "five minutes old" inside the candidate window, and a very small `MAX_QUOTE_AGE_SECONDS` rejects every symbol — the scan then evaluates nothing and (inside the window) exits non-zero. The default is now `1200` seconds; a quote older than that is still used, but the delay is warned about in the log and stated in the alert (`Candle status: still forming — data as of ... IST; provider quote was ~N min behind the exchange`). Quotes older than `HARD_QUOTE_STALE_SECONDS` (3600s) or from a previous session are always skipped as unusable.

Because the 15:15–15:30 IST window is short and provider data is delayed, the scanner also makes a **settled-candle pass**: between the 15:30 IST close and `C_POST_CLOSE_UNTIL` (default **17:00** IST) it evaluates the session's *final* daily bar. That pass uses no intraday data, cannot double-send an alert already sent for the same symbol and session (the candidate ledger and the per-day alert ledger both dedupe), and also lets pending candidates be confirmed/superseded/expired/invalidated on the same evening. The alert says the candle is settled and that the entry reference is that close. Set `C_POST_CLOSE_UNTIL=off` to disable it.

yfinance is not a guaranteed real-time or execution-grade feed; stale/missing intraday data is skipped. No zero-latency or fill-price guarantee is made.

## Rule and data notes

- Candidate candle: bearish or neutral using the shared OB parameters; close strictly above the cycle's 26W level; session on/after the breakout.
- Confirmation: uses the Pine-exact `ob_precision.replay()` engine and its defaults for displacement, origin selection, zone geometry and duplicate handling.
- Breakout: this standalone C scanner derives the 26-week close cross and cross-lock from daily bars. It **does not include the live scanner's c03–c13 weekly momentum gates**. The report says its historical sample is a superset of the live waiting list; add those gates from the existing A/B project before treating the candidate universe as identical.
- Backtest data was eod2 NSE daily OHLCV through 25 Sep 2026. This scanner downloads yfinance data (`auto_adjust=False`). Different OHLCV, corporate-action treatment, missing symbols, or timestamps can change signals. This project does not claim that C reproduces the PDF's Entry-A results.
- The shared `ob_precision.py` and `indicators.py` are vendored unchanged from the matching `atikhalde/weekly` rule engine so C confirmation does not re-implement the precision-OB birth logic. See `UPSTREAM.md`.

## Setup

Python 3.10+ is recommended.

```bash
python -m venv .venv
# Linux/macOS
source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. The initial `symbols.txt` watchlist is RELIANCE, TCS, and INFY. Replace or extend it with your intended NSE universe—without `.NS`—one per line, for example:

```text
RELIANCE
TCS
INFY
```

Create the Telegram bot with BotFather, start a chat with it, and set the chat ID. **Never commit the real `.env` file or bot token.** Optionally set `ACCOUNT_CAPITAL_INR` and `RISK_PER_TRADE_PCT` locally; without capital, the plan still shows per-share risk and estimated R:R but leaves share quantity uncalculated.

## Run safely

Check Yahoo data availability (this tests the provider, not whether the exchange is currently open):

```bash
python entry_c.py --symbols RELIANCE --check-yfinance
```

First test message formatting without sending or persisting alerts:

```bash
python entry_c.py --symbols RELIANCE --test-telegram --dry-run
```

To test actual Telegram delivery, set valid credentials in your **local** `.env`, set `DRY_RUN=0`, then run `python entry_c.py --symbols RELIANCE --test-telegram`. Do not share bot tokens in chat or commit `.env`.

First test the scanner logic without sending or persisting alerts:

```bash
python entry_c.py --symbols RELIANCE,TCS --once --dry-run
```

After confirming the messages and symbols, set `DRY_RUN=0` in `.env` and run continuously during market hours:

```bash
python entry_c.py --poll-seconds 60
```

Or use `--symbols-file symbols.txt`. The scanner checks yfinance data freshness (default maximum age 20 minutes, above the ~15-minute NSE feed delay; adjust with `MAX_QUOTE_AGE_SECONDS`) and skips quotes that are unusable or from a previous session. It maintains its own `state_c.json`; that state is ignored by Git and is not shared with A/B.

To see exactly what the scanner would do for every symbol without sending anything, run the diagnostic probe (it is also available as the `diagnose-entry-c` workflow):

```bash
python tools/diag_probe.py --as-of 15:15 --quote-lag 900 --post-close-at 16:00
```

It prints the raw Yahoo feed delay, each symbol's 26W breakout state, the candle tests, and a replay of the real `scan_pass` at 15:15 IST both with and without an emulated 15-minute provider delay, plus the settled-candle replay after the close.

For a long-running live process, use a VPS/systemd service (example: `precision-c-scanner.service.example`). A separate `scan-entry-c` GitHub Actions workflow now performs best-effort scheduled scans; see below. Scheduled Actions are not a dependable low-latency market scanner.

## GitHub Actions alerts

`test-entry-c` is still tests/compilation only. Its optional connectivity message
is not a trading signal. **`scan-entry-c` actually runs the scanner** every five
minutes on weekdays between 03:00 and 10:59 UTC, skipping closed NSE hours.
Schedules activate only after `.github/workflows/scan.yml` reaches **main**.
You can also use Actions → scan-entry-c → Run workflow on main; select dry run
for a no-send check. Scheduled runs send real qualifying alerts (`DRY_RUN=0`).

Required repository Actions secrets (Settings → Secrets and variables → Actions):

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

These are the same names used by the existing connectivity test. Missing secrets
or an empty symbol list fail the scan visibly. Optional repository **variables**:
`C_SIGNAL_START` (default `15:15` IST), `C_POST_CLOSE_UNTIL` (default `17:00`
IST; `0`/`off` disables the settled-candle pass), `MAX_QUOTE_AGE_SECONDS`
(`1200`, keep it above the ~15-minute Yahoo NSE delay), `ACCOUNT_CAPITAL_INR`,
and `RISK_PER_TRADE_PCT` (1.0). Earlier C windows change strategy timing and
increase provisional-candle risk; they are not required.

Every run writes the per-symbol reasons into the run summary and as run
annotations, so a day with no alert can be explained without downloading the
raw log. `diagnose-entry-c` (Actions → Run workflow) is a read-only probe: it
sends no alerts, never touches the ledger, and replays the window against the
current data.

**Scheduling reality:** the `*/5 3-10 * * 1-5` cron is retained, but GitHub did
not execute a single scheduled run in this repository. Live runs are driven by
whatever dispatches the workflow (`workflow_dispatch`). A dispatcher aligned to
UTC quarter-hours fires at 15:15 IST and 15:30 IST — inside the candidate window
and at the close — which is why the settled-candle pass matters: it gives the
session a second chance on the very next poll.

Each live run restores the latest `entry-c-live-state` artifact and uploads the
updated ledger, including any deliveries completed before a later failure.
Runs are serialized to prevent concurrent sends from stale state. **Do not delete
live-state artifacts**: losing them loses pending candidates and duplicate
history. Artifacts have 90-day retention; archive/restore the ledger before a
long pause. An expired or corrupt ledger fails closed rather than silently
resetting history. If all artifacts have been deleted by retention or a person,
the workflow cannot distinguish that from a first-ever run. Keep an external
backup for long-term operation. If ledger upload fails after a delivery, stop live
scans and recover that run's ledger before retrying: restoring an older artifact
can resend an alert. Telegram network timeouts also cannot guarantee exactly-once
delivery. Do not run a separate VPS sender with a different
ledger against the same watchlist/chat unless duplicate alerts are acceptable.

Logs show each symbol's skip reason (pre-window, missing/other-session data,
delayed quote, no active 26-week breakout, already-confirmed OB, nonqualifying
candle, or tracked candidate) with the numbers behind it, and the run ends with
a one-line tally such as
`Scan complete: 3 symbols; daily_available=3 usable_quotes=3 state_changed=False; no_breakout=3`.
A delayed-but-current-session quote is used with a warning, not treated as a
failure. Complete data outages, unusable intraday data inside the candidate
window, and one-shot exceptions still fail the workflow instead of showing a
misleading green result. No qualifying candidate is a normal successful scan —
and with a watchlist that has no active 26-week breakout, no alert is the
correct outcome.

**Timing limitation:** Actions may queue, delay, or drop scheduled jobs, and
queued concurrent runs may be replaced by GitHub. Even a five-minute schedule
can miss the 15:15–15:30 IST candidate window. Use the systemd/VPS setup above for
more reliable 60-second polling. Yahoo data can also be delayed. No alerts are
forced and the strategy conditions remain unchanged.

## Tests

```bash
python -m unittest discover -s test -v
```

## Repository boundary

This repository is a standalone C test/add-on. It contains no Entry-A or Entry-B strategy code and does not modify your A/B project. To wire C into an existing A/B bot later, pass the active breakout cycle into the C module or consume the existing waiting-list state, then keep C's candidate ledger and alert type separate.
