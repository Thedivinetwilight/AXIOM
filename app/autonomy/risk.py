"""Confirmed-swing stops and dynamic, correlation-aware daily risk controls."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
from typing import Iterable

from app.autonomy.models import AccountSnapshot, Candidate, DailyRisk, Instrument, Position
from app.data.models import Candle


def _finite_positive(*values: float) -> bool:
    return all(math.isfinite(float(v)) and float(v) > 0 for v in values)


def floor_tick(value: float, tick: float) -> float:
    return math.floor((value + tick * 1e-9) / tick) * tick


def ceil_tick(value: float, tick: float) -> float:
    return math.ceil((value - tick * 1e-9) / tick) * tick


def confirmed_swing(candles: Iterable[Candle], side: str, *, lookback: int = 2) -> Candle:
    """Return the latest swing confirmed by completed bars on BOTH sides.

    The final `lookback` bars can only be confirmation; they can never become a
    new swing themselves.  No forming bar is accepted by this function.
    """
    bars = list(candles)
    if lookback < 1 or len(bars) < lookback * 2 + 1:
        raise ValueError("Not enough completed bars to confirm a market-structure swing")
    for index in range(len(bars) - lookback - 1, lookback - 1, -1):
        bar = bars[index]
        neighbourhood = bars[index - lookback:index + lookback + 1]
        if side == "BUY":
            if bar.low <= min(item.low for item in neighbourhood) and \
                    any(bar.low < item.low for item in neighbourhood if item is not bar):
                return bar
        elif side == "SELL":
            if bar.high >= max(item.high for item in neighbourhood) and \
                    any(bar.high > item.high for item in neighbourhood if item is not bar):
                return bar
        else:
            raise ValueError("Side must be BUY or SELL")
    raise ValueError("No confirmed completed-bar swing exists")


def initial_stop(candles: Iterable[Candle], side: str, entry_price: float,
                 tick_size: float, *, lookback: int = 2) -> tuple[float, str]:
    if not _finite_positive(entry_price, tick_size):
        raise ValueError("Valid entry price and tick size are required")
    swing = confirmed_swing(candles, side, lookback=lookback)
    if side == "BUY":
        stop = floor_tick(swing.low - 2 * tick_size, tick_size)
        valid = stop < entry_price
    else:
        stop = ceil_tick(swing.high + 2 * tick_size, tick_size)
        valid = stop > entry_price
    if not valid or not _finite_positive(stop) or abs(entry_price - stop) < 2 * tick_size:
        raise ValueError("Confirmed swing produces an invalid or insufficient stop distance")
    evidence = f"confirmed {swing.timestamp.isoformat()} swing {'low' if side == 'BUY' else 'high'}"
    return stop, evidence


def trailing_stop(candles: Iterable[Candle], side: str, current_stop: float,
                  last_completed_close: float, tick_size: float, *,
                  lookback: int = 2) -> tuple[float, str] | None:
    """Calculate a stop that can only tighten, never cross the last close."""
    if not _finite_positive(current_stop, last_completed_close, tick_size):
        raise ValueError("Invalid current stop or completed close")
    candidate, evidence = initial_stop(
        candles, side,
        last_completed_close - tick_size if side == "BUY" else last_completed_close + tick_size,
        tick_size, lookback=lookback,
    )
    if side == "BUY":
        candidate = min(candidate, floor_tick(last_completed_close - tick_size, tick_size))
        if candidate <= current_stop + tick_size * 1e-8:
            return None
    else:
        candidate = max(candidate, ceil_tick(last_completed_close + tick_size, tick_size))
        if candidate >= current_stop - tick_size * 1e-8:
            return None
    return candidate, evidence


def target_for(side: str, entry: float, stop: float, reward_risk: float,
               tick_size: float) -> float:
    distance = abs(entry - stop)
    if not _finite_positive(entry, stop, distance, reward_risk, tick_size):
        raise ValueError("Invalid stop/target inputs")
    target = entry + distance * reward_risk if side == "BUY" else entry - distance * reward_risk
    target = floor_tick(target, tick_size) if side == "BUY" else ceil_tick(target, tick_size)
    if not _finite_positive(target) or (side == "BUY" and target <= entry) \
            or (side == "SELL" and target >= entry):
        raise ValueError("Invalid protective target")
    return target


@dataclass(frozen=True)
class SizeResult:
    quantity: int
    risk_amount: float
    per_unit_risk: float
    liquidity_cap: int
    reason: str


def stop_sized_quantity(candidate: Candidate, instrument: Instrument, account: AccountSnapshot,
                        daily: DailyRisk, *, bar_volume: float, max_participation: float,
                        gap_risk_fraction: float, cost_rate: float) -> SizeResult:
    """Size from stop distance, costs and gap allowance; never from a fixed count."""
    instrument.validate()
    daily.validate()
    account.validate()
    distance = abs(candidate.entry_price - candidate.stop_price)
    if not _finite_positive(distance, bar_volume, max_participation):
        return SizeResult(0, 0.0, 0.0, 0, "Missing stop distance or liquidity")
    multiplier = float(instrument.contract_multiplier or 0)
    per_unit = distance * multiplier + candidate.entry_price * multiplier * (
        max(0.0, gap_risk_fraction) + max(0.0, cost_rate)
    )
    if not _finite_positive(per_unit):
        return SizeResult(0, 0.0, per_unit, 0, "Invalid per-unit risk")
    remaining = remaining_daily_budget(daily, account.day_pnl)
    budget = min(account.equity * daily.per_trade_risk_fraction, remaining)
    if budget <= 0:
        return SizeResult(0, 0.0, per_unit, 0, "No remaining daily loss budget")
    step = int(instrument.quantity_step)
    risk_cap = int(budget // (per_unit * step)) * step
    liquidity_cap = int((bar_volume * max_participation) // step) * step
    freeze_cap = int(instrument.freeze_quantity // step) * step
    quantity = min(risk_cap, liquidity_cap, freeze_cap)
    if quantity < step:
        return SizeResult(0, 0.0, per_unit, liquidity_cap,
                          "Risk, liquidity or freeze constraints leave less than one contract step")
    return SizeResult(quantity, quantity * per_unit, per_unit, liquidity_cap, "Stop-sized")


def remaining_daily_budget(daily: DailyRisk, day_pnl: float) -> float:
    """Losses consume budget; profits never expand the operator's daily bound."""
    if not math.isfinite(day_pnl):
        return 0.0
    return max(0.0, daily.max_daily_loss + min(0.0, day_pnl))


def daily_loss_breached(daily: DailyRisk, day_pnl: float) -> bool:
    return not math.isfinite(day_pnl) or day_pnl <= -daily.max_daily_loss


def position_risk(position: Position, instrument: Instrument,
                  gap_risk_fraction: float) -> float:
    if position.stop_price is None or not instrument.contract_multiplier:
        raise ValueError(f"Confirmed stop is missing for {position.instrument_key}")
    distance = abs(position.average_price - position.stop_price)
    risk = abs(position.quantity) * instrument.contract_multiplier * (
        distance + abs(position.average_price) * gap_risk_fraction
    )
    if not math.isfinite(risk) or risk < 0:
        raise ValueError("Invalid open-position risk")
    return risk


def correlation_adjusted_risk(items: list[tuple[Position, float]],
                              correlations: dict[tuple[str, str], float]) -> float:
    """Signed portfolio risk with pairwise correlations in [-1, 1].

    Gross risk is checked separately.  Correlation can identify concentrated
    same-direction exposure and must never turn invalid/missing data into a
    diversification benefit.
    """
    variance = 0.0
    for i, (left, left_risk) in enumerate(items):
        if not math.isfinite(left_risk) or left_risk < 0:
            raise ValueError("Invalid portfolio risk component")
        left_sign = 1 if left.quantity > 0 else -1
        variance += left_risk ** 2
        for right, right_risk in items[i + 1:]:
            right_sign = 1 if right.quantity > 0 else -1
            key = tuple(sorted((left.instrument_key, right.instrument_key)))
            rho = correlations.get(key)
            if rho is None:
                # Missing evidence is concentrated risk, never a free
                # diversification assumption.  A measured correlation must be
                # supplied explicitly by the chronological research/runtime
                # estimator before it can reduce portfolio risk.
                rho = 1.0
            if not math.isfinite(rho) or not -1 <= rho <= 1:
                raise ValueError("Invalid correlation input")
            variance += 2 * rho * left_sign * right_sign * left_risk * right_risk
    return math.sqrt(max(0.0, variance))


@dataclass(frozen=True)
class PortfolioDecision:
    allowed: bool
    reasons: tuple[str, ...]
    gross_risk: float
    correlated_risk: float
    remaining_daily_loss: float
    daily_loss_blocked: bool


def portfolio_entry_check(candidate: Candidate, sized: SizeResult, instrument: Instrument,
                          account: AccountSnapshot, daily: DailyRisk,
                          instruments: dict[str, Instrument], *,
                          required_margin: float, bid: float, ask: float,
                          quote_time: datetime, now: datetime, stale_after_seconds: float,
                          max_spread_bps: float, max_margin_usage_fraction: float,
                          gap_risk_fraction: float,
                          correlations: dict[tuple[str, str], float]) -> PortfolioDecision:
    reasons: list[str] = []
    remaining = remaining_daily_budget(daily, account.day_pnl)
    blocked = daily_loss_breached(daily, account.day_pnl)
    if blocked:
        reasons.append("Daily loss bound reached: new entries blocked; protection continues")
    if sized.quantity <= 0:
        reasons.append(sized.reason)
    if not _finite_positive(required_margin, bid, ask) or ask < bid:
        reasons.append("Valid broker margin and bid/ask are required")
    elif (ask - bid) / ((ask + bid) / 2) * 10_000 > max_spread_bps:
        reasons.append("Spread exceeds the configured liquidity bound")
    age = (now - quote_time.astimezone(now.tzinfo)).total_seconds()
    if not math.isfinite(age) or age < -1 or age > stale_after_seconds:
        reasons.append("Execution quote is stale or from the future")
    if required_margin > account.available_margin:
        reasons.append("Insufficient available broker margin")
    if account.used_margin + required_margin > account.equity * max_margin_usage_fraction:
        reasons.append("Aggregate margin utilization bound would be exceeded")
    if candidate.entry_price < bid or candidate.entry_price > ask:
        # A limit may lie inside/on the spread, never outside the currently
        # observable executable bounds in the direction that assumes a fill.
        if not bid <= candidate.entry_price <= ask:
            reasons.append("Entry limit is outside the fresh quoted spread")

    risk_items: list[tuple[Position, float]] = []
    gross = 0.0
    try:
        for position in account.positions:
            if not position.quantity:
                continue
            existing_instrument = instruments.get(position.instrument_key)
            if existing_instrument is None:
                raise ValueError(f"Unknown open instrument {position.instrument_key}")
            risk = position_risk(position, existing_instrument, gap_risk_fraction)
            gross += risk
            risk_items.append((position, risk))
        new_position = Position(
            instrument_key=candidate.instrument_key,
            quantity=sized.quantity if candidate.side == "BUY" else -sized.quantity,
            average_price=candidate.entry_price,
            last_price=candidate.entry_price,
            stop_price=candidate.stop_price,
            correlation_group=instrument.correlation_group,
        )
        gross += sized.risk_amount
        risk_items.append((new_position, sized.risk_amount))
        correlated = correlation_adjusted_risk(risk_items, correlations)
        if gross > remaining + 1e-7:
            reasons.append("Aggregate stop/gap risk exceeds remaining daily loss budget")
        if correlated > remaining + 1e-7:
            reasons.append("Correlation-adjusted risk exceeds remaining daily loss budget")
    except ValueError as exc:
        reasons.append(str(exc))
        correlated = gross
    return PortfolioDecision(not reasons, tuple(reasons), gross, correlated, remaining, blocked)
