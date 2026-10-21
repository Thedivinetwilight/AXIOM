"""Test doubles shared by engine integration tests.

The passing net-edge double isolates tests of downstream routing, persistence,
and safety behavior from the separately tested real edge calculation. It is
not a forecast and must never be used by the application.
"""

from types import SimpleNamespace


class PassingNetEdgeGate:
    """Explicit test-only pass for integrations that are not testing edge."""

    def evaluate(self, **_kwargs):
        return SimpleNamespace(
            passes=True,
            gross_expected=1.0,
            fees=0.0,
            spread_cost=0.0,
            slippage=0.0,
            market_impact=0.0,
            net_expected=1.0,
            threshold=0.0,
            threshold_amount=0.0,
            reason="test-only edge-gate stub; not forecast evidence",
        )
