# Shared OB-engine provenance

`ob_precision.py` and `indicators.py` are copied unchanged for the C confirmation check from the matching public rule engine in `atikhalde/weekly`:

- https://github.com/atikhalde/weekly/blob/main/ob_precision.py
- https://github.com/atikhalde/weekly/blob/main/indicators.py

At the time of retrieval, the `weekly` main HEAD was `47694d7ac484261fc8b0514356c00eca8907c266`. The C scanner uses `OBParams()` defaults and calls `replay()`; it does not change the shared engine or any A/B entry logic.

If the upstream engine changes, review those changes and update this snapshot deliberately. The breakout-cycle helper in `entry_c.py` is a separate small implementation; it does not include the c03–c13 weekly momentum gates noted in the backtest report.
