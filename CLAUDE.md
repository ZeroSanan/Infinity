# Project notes for Claude

## Keep signal_lab in sync with the live signal system

`signal_lab/signal_fn.py` is a standalone, side-effect-free COPY of the
regime/entry decision logic in `core/regime_live.py` (used by
`core/mixed_engine.py`). It powers the Backtester's research tools:

- Signal Replay / Point-in-Time Test (`POST /api/backtest/replay`)
- Signal Scan (`POST /api/backtest/signal-scan`, via `signal_lab/harness.py`)

**Whenever `core/regime_live.py`'s regime score, confirmation/override state
machine, or entry indicator (`compute_regime_state`, `score_entry`, related
helpers) is changed, mirror the equivalent change into
`signal_lab/signal_fn.py`.** Otherwise these tools will silently report
verdicts that no longer match what the live engine would actually do.

`signal_lab/harness.py` and `signal_lab/klines.py` generally don't need
changes for live-logic tweaks — they just call `signal_fn.get_verdict()` —
unless `get_verdict()`'s return shape (keys used by the UI/route) changes too.
