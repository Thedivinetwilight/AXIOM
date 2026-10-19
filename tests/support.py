"""Test doubles shared by engine integration tests.

The passing net-edge double isolates tests of downstream routing, persistence,
and safety behavior from the separately tested real edge calculation. It is
not a forecast and must never be used by the application.
"""

from types import SimpleNamespace


class PassingNetEdgeGate:
    """Test-only downstream isolation; never supplies expected-return evidence."""

    def evaluate(self, **_kwargs):
        return SimpleNamespace(
            passes=True,
            gross_expected=None,
            fees=None,
            spread_cost=None,
            slippage=None,
            market_impact=None,
            net_expected=None,
            threshold=0.0,
            threshold_amount=None,
            reason="test-only downstream isolation; no forecast evidence",
        )
