"""AXIOM — the desktop UI / orchestration layer for Arena 3.0.5.

Layout:

* ``axiom.models``     — Kronos/forecast domain models (separate from Arena state)
* ``axiom.market``     — MarketDataProvider abstraction, validation, calendars,
                         closed-candle gate, replay + real providers
* ``axiom.forecast``   — cache identity, forecast validation, ForecastStore,
                         forecast pipeline
* ``axiom.kronos``     — Kronos loader/inference wrapper, model cache, sampling
* ``axiom.worker``     — process isolation (AXIOM ↔ AXIOM-Kronos IPC)
* ``axiom.research``   — walk-forward evaluation against naive baselines

Hard boundaries:

1. Nothing in ``axiom`` imports ``app`` (Arena). Kronos never reads or writes
   Arena's authoritative state, decisions, risk or account snapshots.
2. Kronos never places orders, never changes risk limits, never enables LIVE
   trading. It produces forecasts that the UI labels as forecasts.
3. Forecast data never masquerades as market data (``origin=KRONOS_FORECAST``,
   ``forecast_*`` field names).
"""

__version__ = "0.1.0"
