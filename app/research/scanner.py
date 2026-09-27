"""OpportunityScanner: quality-ranked opportunities over the eligible set.

quality = validated_edge_fraction − cost_drag_fraction
          − illiquidity penalty (0.01) − portfolio-heat penalty (0.10 × exposure)
Only candidates with RECORDED positive out-of-sample evidence (expectancy > 0,
≥3 OOS trades) can appear; no evidence ⇒ no opportunity (never guessed).
Output is RESEARCH ONLY — trading still requires the hard risk gate.
"""

from __future__ import annotations

from statistics import fmean

from app.strategies.base import Signal


def cost_drag(price: float, quantity: float, commission: float, slippage: float,
              spread: float) -> float:
    """Round-trip cost for a proposed position (documented model)."""
    return 2.0 * (commission + slippage + spread / 2.0) * price * quantity


class OpportunityScanner:
    def __init__(self, repository, store, *, settings,
                 regime_engine=None, calibration=None):
        self.repository = repository
        self.store = store
        self.settings = settings
        self.regime_engine = regime_engine
        self.calibration = calibration

    def scan(self, instrument_keys: list[str], strategies: list, *,
             unit: str = "days", interval: int = 1, lookback: int = 60,
             evidence: dict[str, dict] | None = None, top_k: int | None = None,
             current_exposure: float = 0.0) -> list[dict]:
        evidence = evidence or {}
        top_k = top_k or int(getattr(self.settings, "scanner_top_k", 5))
        opportunities = []
        from app.learning.regime_engine import features

        for key in instrument_keys:
            rows = self.store.read_range(key, unit, int(interval))[-lookback:]
            if len(rows) < 10:
                continue  # not enough local evidence — skipped, never guessed
            candles = []
            from app.data.models import Candle as CandleModel
            from app.history.store import parse_ts

            symbol = key.split("|")[-1]
            for row in rows:
                candles.append(CandleModel(parse_ts(row["ts"]), row["open"],
                                           row["high"], row["low"], row["close"],
                                           row["volume"], symbol))
            feats = features(candles)
            liquidity = fmean(r["volume"] * r["close"] for r in rows[-10:])
            last_price = candles[-1].close
            for strategy in strategies:
                limit = max(getattr(strategy, "history_limit", 30), 5)
                signal = strategy.generate_signal(tuple(candles[-limit:]))
                if signal == Signal.HOLD:
                    continue
                recorded = evidence.get(strategy.name) or {}
                edge = float(recorded.get("oos_expectancy") or 0.0)
                trades = int(recorded.get("oos_trades") or 0)
                if edge <= 0 or trades < 3:
                    continue  # no validated edge → not an opportunity
                drag = cost_drag(last_price, 1.0,
                                 self.settings.commission, self.settings.slippage,
                                 self.settings.spread) / max(last_price, 1e-9)
                edge_fraction = edge / max(last_price, 1e-9)
                quality = edge_fraction - drag
                if liquidity <= 0:
                    quality -= 0.01  # illiquid: penalize, never reject silently
                quality -= 0.10 * current_exposure  # portfolio heat penalty
                detail = {"signal": signal.value,
                          "regime": self.regime_engine.current
                          if self.regime_engine else None,
                          "features": {k: feats.get(k) for k in
                                       ("trend", "volatility", "momentum")},
                          "liquidity": liquidity, "cost_drag": drag,
                          "edge_per_unit": edge, "exposure": current_exposure}
                scan_id = self.repository.record_opportunity(
                    key, strategy.name, signal.value, quality, detail)
                opportunities.append({"scan_id": scan_id, "instrument_key": key,
                                      "strategy": strategy.name,
                                      "action": signal.value, "score": quality,
                                      **detail})
        opportunities.sort(key=lambda item: item["score"], reverse=True)
        return opportunities[:top_k]
