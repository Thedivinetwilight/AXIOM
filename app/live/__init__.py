"""Live operations (V2.1 L-ops): the safety rails around real-money routing.

Everything in this package is about running a live engine *safely*, not about
placing orders:

- :mod:`app.live.alerts`    — alert routing (durable + optional webhook)
- :mod:`app.live.guards`    — pre-entry guards (token freshness, canary ramp)
- :mod:`app.live.watchdog`  — observation (stuck orders, stalls) + breaches
- :mod:`app.live.preflight` — offline readiness checks
- :mod:`app.live.ops`       — the facade the engine talks to

Guards apply to NEW ENTRIES only; exits are never blocked.
"""

from app.live.alerts import Alert, AlertRouter, recent_alerts
from app.live.guards import CanaryGuard, GuardDecision, TokenGuard
from app.live.ops import LiveOperations
from app.live.watchdog import Breach, BreachMonitor, Finding, LiveWatchdog

__all__ = [
    "Alert", "AlertRouter", "recent_alerts",
    "CanaryGuard", "GuardDecision", "TokenGuard",
    "LiveOperations",
    "Breach", "BreachMonitor", "Finding", "LiveWatchdog",
]
