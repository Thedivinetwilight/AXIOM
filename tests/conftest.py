from datetime import datetime, timedelta, timezone
import pytest
from app.data.models import Candle
from app.backtesting.costs import ExecutionConfig
from scripts.make_sample_data import sample


@pytest.fixture
def bars():
    return sample(count=300)


class _DeterministicResourceGovernor:
    """Test-only pressure source; never samples the host running CI."""

    def __init__(self, *, pressure: bool):
        self._pressure = bool(pressure)
        self.trading_critical = False

    def under_pressure(self) -> bool:
        return self._pressure


@pytest.fixture
def quota_admission_client(client):
    """Isolate quota tests from real CI-host load/disk pressure.

    Pressure admission has separate explicit tests using a pressured governor;
    quota tests should measure quota behavior, not the runner's transient disk.
    """
    workstation = client.app.state.workstation
    workstation.research_scheduler.governor = _DeterministicResourceGovernor(
        pressure=False)
    return client


@pytest.fixture
def pressure_admission_client(client):
    """Exercise the real HTTP refusal path under explicit resource pressure."""
    workstation = client.app.state.workstation
    workstation.research_scheduler.governor = _DeterministicResourceGovernor(
        pressure=True)
    return client


@pytest.fixture
def zero_cost():
    return ExecutionConfig(
        initial_capital=1000,
        commission=0,
        slippage=0,
        spread=0,
        position_size=1,
        stop_loss=None,
        take_profit=None,
        max_participation=1,
    )


def candle(day=0, price=100, *, opening=None, high=None, low=None, volume=10000, symbol="DEMO"):
    opening = price if opening is None else opening
    return Candle(
        datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(days=day),
        opening,
        high if high is not None else max(opening, price),
        low if low is not None else min(opening, price),
        price,
        volume,
        symbol,
    )
