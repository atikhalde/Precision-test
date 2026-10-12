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

An end-of-day alert cannot guarantee execution at the just-printed close. The default alert window begins at **15:15 Asia/Kolkata** so a candidate can be noticed before the NSE close; its candle shape and price are still provisional. Earlier alerts are possible by setting `C_SIGNAL_START` earlier, but increase false candidates. yfinance is not a guaranteed real-time or execution-grade feed; stale/missing intraday data is skipped. No zero-latency or fill-price guarantee is made.

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

Edit `.env` and set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. Add NSE symbols—without `.NS`—one per line in `symbols.txt`, for example:

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

Or use `--symbols-file symbols.txt`. The scanner checks yfinance data freshness (default maximum age 5 minutes; adjust with `MAX_QUOTE_AGE_SECONDS`) and will skip stale intraday snapshots. It maintains its own `state_c.json`; that state is ignored by Git and is not shared with A/B.

For a long-running live process, use a VPS/systemd service (example: `precision-c-scanner.service.example`). GitHub Actions is included only for tests; scheduled Actions are not a dependable low-latency market scanner.

## Tests

```bash
python -m unittest discover -s tests -v
```

## Repository boundary

This repository is a standalone C test/add-on. It contains no Entry-A or Entry-B strategy code and does not modify your A/B project. To wire C into an existing A/B bot later, pass the active breakout cycle into the C module or consume the existing waiting-list state, then keep C's candidate ledger and alert type separate.
