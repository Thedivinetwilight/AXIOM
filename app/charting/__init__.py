"""Charting for the trading terminal: aggregation, overlays and markers.

Import order matters if you add modules here: ``series`` has no dependency on
the rest of the package, ``markers`` depends on ``series``, and ``source``
depends on both plus the data layer.
"""

from app.charting.markers import order_markers, position_levels, trade_markers
from app.charting.series import (
    OVERLAYS,
    OSCILLATORS,
    ChartRequest,
    ResampleError,
    bar_payload,
    bucket_index,
    can_aggregate,
    oscillator_series,
    overlay_series,
    resample,
    series_bounds,
)
from app.charting.source import ResolvedSeries, dataset_index, engine_bars, resolve

__all__ = [
    "ChartRequest",
    "OSCILLATORS",
    "OVERLAYS",
    "ResampleError",
    "ResolvedSeries",
    "bar_payload",
    "bucket_index",
    "can_aggregate",
    "dataset_index",
    "engine_bars",
    "order_markers",
    "oscillator_series",
    "overlay_series",
    "position_levels",
    "resample",
    "resolve",
    "series_bounds",
    "trade_markers",
]
