"""Auditable corporate-action handling for AXIOM research and backtesting.

Scope: **stock splits** and **cash dividends** only. Every other corporate action
(bonus issues, rights issues, mergers, demergers, spin-offs, delistings, symbol
changes) is *recognised by name* so it can be labelled explicitly, but it is
**never applied** to prices — an unsupported action downgrades the affected series
to ``PARTIAL_UNSUPPORTED`` and adds a machine-readable limitation.

Import format (versioned, provider-neutral JSON — see
``docs/CORPORATE_ACTIONS.md``)::

    {
      "format": "axiom.corporate-actions",
      "format_version": 1,
      "provider": {"name": "...", "version": "...", "notes": "..."},
      "actions": [
        {"record_id": "NSE-DEMO-SPLIT-2024-06-14", "revision": 1,
         "market": "NSE", "symbol": "DEMO", "action_type": "SPLIT",
         "ex_date": "2024-06-14", "announced_at": "2024-05-02",
         "ratio_from": 1, "ratio_to": 2,
         "source": "NSE corporate-action filing", "source_version": "2024-06-14",
         "provenance": "operator transcription of the exchange filing"},
        {"record_id": "NSE-DEMO-DIV-2024-07-05", "revision": 1,
         "market": "NSE", "symbol": "DEMO", "action_type": "CASH_DIVIDEND",
         "ex_date": "2024-07-05", "announced_at": "2024-05-20",
         "cash_per_share": 5.0, "currency": "INR",
         "source": "...", "source_version": "...", "provenance": "..."}
      ]
    }

Conventions implemented here (documented in ``docs/CORPORATE_ACTIONS.md``):

* **Splits** — economic value is preserved: for bars strictly *before* the ex-date,
  ``price /= (ratio_to / ratio_from)`` and ``volume *= (ratio_to / ratio_from)``;
  a simulated position keeps its value by ``qty *= factor`` and ``avg_price /= factor``.
* **Dividends, two mutually exclusive conventions**
  - ``TOTAL_RETURN_ADJUSTED``: the dividend is folded into prices
    (``P_adj = P * (1 - cash / prev_close)`` for bars before the ex-date) and
    **no cash is credited**.
  - ``RAW`` / ``SPLIT_ADJUSTED``: prices are untouched and the dividend is exposed
    as a **cash event** for a research cash ledger. No price adjustment happens.
  The two can never be enabled together; :func:`assert_no_double_counting` raises.
* **Look-ahead** — an action only influences a simulated date when it was already
  known and effective then: ``ex_date <= as_of`` *and* (when supplied)
  ``announced_at <= as_of``. A record announced or effective after ``as_of`` is
  invisible to that simulation.

Nothing in this module touches brokers, orders, sessions or live execution.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any, Iterable

from app.data.models import Candle, parse_timestamp

FORMAT_ID = "axiom.corporate-actions"
SUPPORTED_FORMAT_VERSIONS: tuple[int, ...] = (1,)

SPLIT = "SPLIT"
CASH_DIVIDEND = "CASH_DIVIDEND"
SUPPORTED_ACTION_TYPES: tuple[str, ...] = (SPLIT, CASH_DIVIDEND)

# Recognised but deliberately unsupported: labelled, never applied.
UNSUPPORTED_ACTION_TYPES: tuple[str, ...] = (
    "BONUS_ISSUE",
    "RIGHTS_ISSUE",
    "MERGER",
    "DEMERGER",
    "SPIN_OFF",
    "DELISTING",
    "SYMBOL_CHANGE",
)

UNSUPPORTED_ACTION_NOTE = (
    "Recognised corporate action types OUTSIDE this release's scope. They are "
    "recorded, labelled and excluded from every price/volume/cash adjustment; "
    "any research window containing one is reported as PARTIAL_UNSUPPORTED."
)

RESEARCH_MARKETS: tuple[str, ...] = ("NSE", "NASDAQ")
MARKET_CURRENCY = {"NSE": "INR", "NASDAQ": "USD"}


# --------------------------------------------------------------------------- #
# Errors / enums
# --------------------------------------------------------------------------- #
class CorporateActionError(ValueError):
    """Base class for every corporate-action data problem."""


class MalformedCorporateAction(CorporateActionError):
    """A record is structurally invalid (missing/invalid fields, bad version)."""


class DuplicateCorporateAction(CorporateActionError):
    """The same record id/revision was supplied twice."""


class ConflictingCorporateAction(CorporateActionError):
    """Two records make incompatible claims about the same effective event."""


class AmbiguousCorporateAction(CorporateActionError):
    """The payload cannot be applied unambiguously (e.g. two splits, same day)."""


class AdjustmentNotDefensible(CorporateActionError):
    """The data on hand do not support a defensible adjustment for this action."""


class AdjustedSeriesError(CorporateActionError):
    """An adjusted (research) series was about to be used as execution prices."""


class AdjustmentPolicy(StrEnum):
    """How the research series is built relative to the raw traded prices."""

    RAW = "RAW"
    SPLIT_ADJUSTED = "SPLIT_ADJUSTED"
    TOTAL_RETURN_ADJUSTED = "TOTAL_RETURN_ADJUSTED"


class AdjustmentStatus(StrEnum):
    """Dataset/series label recorded in manifests and reports."""

    RAW = "RAW"
    SPLIT_ADJUSTED = "SPLIT_ADJUSTED"
    DIVIDEND_ADJUSTED = "DIVIDEND_ADJUSTED"
    PARTIAL_UNSUPPORTED = "PARTIAL_UNSUPPORTED"
    UNVERIFIED = "UNVERIFIED"


POLICY_STATUS = {
    AdjustmentPolicy.RAW: AdjustmentStatus.RAW,
    AdjustmentPolicy.SPLIT_ADJUSTED: AdjustmentStatus.SPLIT_ADJUSTED,
    AdjustmentPolicy.TOTAL_RETURN_ADJUSTED: AdjustmentStatus.DIVIDEND_ADJUSTED,
}


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #
def _as_utc_date(value: Any, field_name: str) -> datetime:
    """Accept ``YYYY-MM-DD``, an ISO timestamp, date or datetime; return UTC midnight."""
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, date):
        moment = datetime(value.year, value.month, value.day, tzinfo=UTC)
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        try:
            if len(text) == 10:
                moment = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=UTC)
            else:
                moment = parse_timestamp(text)
        except (TypeError, ValueError) as exc:
            raise MalformedCorporateAction(f"{field_name} is not an ISO date: {value!r}") from exc
    else:
        raise MalformedCorporateAction(f"{field_name} is required (YYYY-MM-DD)")
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise MalformedCorporateAction(f"{field_name} must be timezone-aware or a plain date")
    return moment.astimezone(UTC)


def _positive_float(value: Any, field_name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise MalformedCorporateAction(f"{field_name} must be a number") from exc
    if not number > 0 or number != number or number in (float("inf"), float("-inf")):
        raise MalformedCorporateAction(f"{field_name} must be a finite number > 0")
    return number


def _required_text(value: Any, field_name: str, *, max_len: int = 200) -> str:
    text = str(value or "").strip()
    if not text:
        raise MalformedCorporateAction(f"{field_name} is required")
    if len(text) > max_len:
        raise MalformedCorporateAction(f"{field_name} is too long (max {max_len})")
    return text


@dataclass(frozen=True)
class CorporateAction:
    """One validated corporate-action record (already normalised)."""

    record_id: str
    market: str
    symbol: str
    action_type: str
    ex_date: datetime
    source: str
    source_version: str
    provenance: str
    revision: int = 1
    announced_at: datetime | None = None
    ratio_from: float | None = None
    ratio_to: float | None = None
    cash_per_share: float | None = None
    currency: str | None = None
    notes: str = ""

    # -- derived helpers ---------------------------------------------------
    @property
    def supported(self) -> bool:
        return self.action_type in SUPPORTED_ACTION_TYPES

    @property
    def split_factor(self) -> float:
        if self.action_type != SPLIT:
            raise CorporateActionError("split_factor is only defined for SPLIT")
        assert self.ratio_from is not None and self.ratio_to is not None
        return float(self.ratio_to) / float(self.ratio_from)

    def effective_values(self) -> dict[str, Any]:
        """The economic payload that defines this event (used for conflict detection)."""
        if self.action_type == SPLIT:
            return {"ratio_from": self.ratio_from, "ratio_to": self.ratio_to}
        return {"cash_per_share": self.cash_per_share, "currency": self.currency}

    def canonical(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "record_id": self.record_id,
            "revision": self.revision,
            "market": self.market,
            "symbol": self.symbol,
            "action_type": self.action_type,
            "ex_date": self.ex_date.isoformat(),
            "announced_at": self.announced_at.isoformat() if self.announced_at else None,
            "source": self.source,
            "source_version": self.source_version,
            "provenance": self.provenance,
            **self.effective_values(),
        }
        if self.notes:
            payload["notes"] = self.notes
        return payload

    def to_dict(self) -> dict[str, Any]:
        return self.canonical()

    def known_at(self, as_of: datetime) -> bool:
        """Point-in-time visibility: effective *and* already announced at ``as_of``."""
        if self.ex_date > as_of:
            return False
        if self.announced_at is not None and self.announced_at > as_of:
            return False
        return True


class CorporateActionsBook:
    """A validated, hash-identified set of corporate actions for one provider feed."""

    def __init__(
        self,
        actions: Iterable[CorporateAction],
        *,
        provider: dict[str, Any],
        source_sha256: str = "",
        superseded: Iterable[CorporateAction] = (),
        format_version: int = 1,
    ) -> None:
        self._actions = tuple(sorted(actions, key=lambda a: (a.symbol, a.ex_date, a.record_id)))
        self.provider = dict(provider)
        self.source_sha256 = source_sha256
        self.superseded = tuple(superseded)
        self.format_version = int(format_version)

    # -- construction ------------------------------------------------------
    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any] | str | bytes,
        *,
        source_sha256: str | None = None,
    ) -> "CorporateActionsBook":
        raw_bytes: bytes | None = None
        if isinstance(payload, (str, bytes)):
            raw_bytes = payload.encode("utf-8") if isinstance(payload, str) else bytes(payload)
            try:
                data = json.loads(raw_bytes.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise MalformedCorporateAction("Corporate-action payload is not valid JSON") from exc
        elif isinstance(payload, dict):
            data = payload
        else:
            raise MalformedCorporateAction(
                "Corporate-action payload must be a JSON object, string or bytes"
            )

        if not isinstance(data, dict):
            raise MalformedCorporateAction("Corporate-action payload must be a JSON object")
        if str(data.get("format") or "") != FORMAT_ID:
            raise MalformedCorporateAction(
                f"Unsupported corporate-action format {data.get('format')!r}; expected {FORMAT_ID!r}"
            )
        try:
            version = int(data.get("format_version"))
        except (TypeError, ValueError) as exc:
            raise MalformedCorporateAction("format_version is required and must be an integer") from exc
        if version not in SUPPORTED_FORMAT_VERSIONS:
            raise MalformedCorporateAction(
                f"Unsupported corporate-action format_version {version}; "
                f"supported: {list(SUPPORTED_FORMAT_VERSIONS)}"
            )

        provider_raw = data.get("provider")
        if not isinstance(provider_raw, dict):
            raise MalformedCorporateAction("provider block is required (name/version)")
        provider = {
            "name": _required_text(provider_raw.get("name"), "provider.name"),
            "version": _required_text(provider_raw.get("version"), "provider.version"),
            "notes": str(provider_raw.get("notes") or "")[:500],
        }

        actions_raw = data.get("actions")
        if actions_raw is None:
            actions_raw = []
        if not isinstance(actions_raw, list):
            raise MalformedCorporateAction("actions must be a list")

        parsed: list[CorporateAction] = []
        for index, item in enumerate(actions_raw):
            if not isinstance(item, dict):
                raise MalformedCorporateAction(f"actions[{index}] must be an object")
            parsed.append(cls._parse_one(item, index))

        actions, superseded = cls._resolve_revisions(parsed)
        cls._reject_ambiguity(actions)

        if raw_bytes is not None:
            digest = hashlib.sha256(raw_bytes).hexdigest()
        else:
            digest = hashlib.sha256(
                json.dumps(data, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
            ).hexdigest()
        return cls(
            actions,
            provider=provider,
            source_sha256=source_sha256 or digest,
            superseded=superseded,
            format_version=version,
        )

    @staticmethod
    def _parse_one(item: dict[str, Any], index: int) -> CorporateAction:
        where = f"actions[{index}]"
        record_id = _required_text(item.get("record_id"), f"{where}.record_id")
        market = _required_text(item.get("market"), f"{where}.market").upper()
        if market not in RESEARCH_MARKETS:
            raise MalformedCorporateAction(
                f"{where}.market {market!r} is not a research market {list(RESEARCH_MARKETS)}"
            )
        symbol = _required_text(item.get("symbol"), f"{where}.symbol").upper()
        action_type = _required_text(item.get("action_type"), f"{where}.action_type").upper()
        if action_type not in SUPPORTED_ACTION_TYPES + UNSUPPORTED_ACTION_TYPES:
            raise MalformedCorporateAction(
                f"{where}.action_type {action_type!r} is not recognised; supported: "
                f"{list(SUPPORTED_ACTION_TYPES)}, recognised-unsupported: {list(UNSUPPORTED_ACTION_TYPES)}"
            )
        ex_date = _as_utc_date(item.get("ex_date"), f"{where}.ex_date")
        revision_raw = item.get("revision", 1)
        try:
            revision = int(revision_raw)
        except (TypeError, ValueError) as exc:
            raise MalformedCorporateAction(f"{where}.revision must be an integer") from exc
        if revision < 1:
            raise MalformedCorporateAction(f"{where}.revision must be >= 1")

        announced_raw = item.get("announced_at")
        announced_at = _as_utc_date(announced_raw, f"{where}.announced_at") if announced_raw else None
        if announced_at is not None and announced_at > ex_date and revision == 1:
            # A first publication dated after the event it describes is a data error.
            # Corrections (revision >= 2) are legitimately published after the ex-date.
            raise MalformedCorporateAction(
                f"{where}.announced_at ({announced_at.date()}) is after ex_date ({ex_date.date()}); "
                "only a correction (revision >= 2) may be published after the event"
            )

        source = _required_text(item.get("source"), f"{where}.source")
        source_version = _required_text(item.get("source_version"), f"{where}.source_version")
        provenance = _required_text(item.get("provenance"), f"{where}.provenance")

        ratio_from = ratio_to = cash = currency = None
        if action_type == SPLIT:
            ratio_from = _positive_float(item.get("ratio_from"), f"{where}.ratio_from")
            ratio_to = _positive_float(item.get("ratio_to"), f"{where}.ratio_to")
            if ratio_from == ratio_to:
                raise MalformedCorporateAction(
                    f"{where} SPLIT ratio_from and ratio_to are equal ({ratio_from}); not a split"
                )
            if item.get("cash_per_share") not in (None, ""):
                raise MalformedCorporateAction(f"{where} SPLIT must not carry cash_per_share")
        elif action_type == CASH_DIVIDEND:
            cash = _positive_float(item.get("cash_per_share"), f"{where}.cash_per_share")
            currency = _required_text(item.get("currency"), f"{where}.currency").upper()
            expected = MARKET_CURRENCY[market]
            if currency != expected:
                raise MalformedCorporateAction(
                    f"{where}.currency {currency!r} does not match market {market} ({expected})"
                )
            if item.get("ratio_from") not in (None, "") or item.get("ratio_to") not in (None, ""):
                raise MalformedCorporateAction(f"{where} CASH_DIVIDEND must not carry split ratios")
        else:
            # Recognised but unsupported: keep the record, drop any economic payload.
            if item.get("ratio_from") not in (None, "") or item.get("cash_per_share") not in (None, ""):
                raise MalformedCorporateAction(
                    f"{where} unsupported action {action_type} must not carry adjustment values"
                )

        return CorporateAction(
            record_id=record_id,
            market=market,
            symbol=symbol,
            action_type=action_type,
            ex_date=ex_date,
            source=source,
            source_version=source_version,
            provenance=provenance,
            revision=revision,
            announced_at=announced_at,
            ratio_from=ratio_from,
            ratio_to=ratio_to,
            cash_per_share=cash,
            currency=currency,
            notes=str(item.get("notes") or "")[:500],
        )

    @staticmethod
    def _resolve_revisions(
        parsed: list[CorporateAction],
    ) -> tuple[list[CorporateAction], list[CorporateAction]]:
        """Keep the newest revision per record_id; a corrected record supersedes the old one."""
        by_id: dict[str, CorporateAction] = {}
        superseded: list[CorporateAction] = []
        for action in parsed:
            existing = by_id.get(action.record_id)
            if existing is None:
                by_id[action.record_id] = action
                continue
            if existing.revision == action.revision:
                if existing.canonical() != action.canonical():
                    raise DuplicateCorporateAction(
                        f"record_id {action.record_id!r} revision {action.revision} was supplied twice "
                        "with different effective values"
                    )
                raise DuplicateCorporateAction(
                    f"record_id {action.record_id!r} revision {action.revision} was supplied twice"
                )
            winner, loser = (
                (action, existing) if action.revision > existing.revision else (existing, action)
            )
            by_id[action.record_id] = winner
            superseded.append(loser)
        return list(by_id.values()), superseded

    @staticmethod
    def _reject_ambiguity(actions: list[CorporateAction]) -> None:
        """Two *different* records claiming the same effective event are ambiguous/conflicting."""
        seen: dict[tuple[str, str, datetime, str], CorporateAction] = {}
        for action in actions:
            if not action.supported:
                continue
            key = (action.market, action.symbol, action.ex_date, action.action_type)
            other = seen.get(key)
            if other is None:
                seen[key] = action
                continue
            if other.effective_values() != action.effective_values():
                raise ConflictingCorporateAction(
                    f"{action.market}/{action.symbol} {action.action_type} on "
                    f"{action.ex_date.date()}: records {other.record_id!r} and "
                    f"{action.record_id!r} disagree ({other.effective_values()} vs "
                    f"{action.effective_values()})"
                )
            raise AmbiguousCorporateAction(
                f"{action.market}/{action.symbol} {action.action_type} on {action.ex_date.date()} "
                f"is declared by both {other.record_id!r} and {action.record_id!r}"
            )

    # -- queries -----------------------------------------------------------
    @property
    def actions(self) -> tuple[CorporateAction, ...]:
        return self._actions

    def supported_actions(self) -> tuple[CorporateAction, ...]:
        return tuple(a for a in self._actions if a.supported)

    def unsupported_actions(self) -> tuple[CorporateAction, ...]:
        return tuple(a for a in self._actions if not a.supported)

    def for_instrument(self, market: str = "", symbol: str = "") -> "CorporateActionsBook":
        """Narrow the book to one instrument; an empty criterion matches everything."""
        mkt, sym = market.strip().upper(), symbol.strip().upper()
        keep = [
            a
            for a in self._actions
            if (not mkt or a.market == mkt) and (not sym or a.symbol == sym)
        ]
        return CorporateActionsBook(
            keep,
            provider=self.provider,
            source_sha256=self.source_sha256,
            superseded=[
                s
                for s in self.superseded
                if (not mkt or s.market == mkt) and (not sym or s.symbol == sym)
            ],
            format_version=self.format_version,
        )

    def for_symbol(self, market: str, symbol: str) -> "CorporateActionsBook":
        return self.for_instrument(market, symbol)

    def as_of(self, moment: datetime) -> "CorporateActionsBook":
        """Point-in-time view: the revision current at ``moment``, if already public.

        Superseded revisions are kept precisely so that a simulation at an earlier
        date sees the values known *then* — a later correction cannot retroactively
        change a decision that had already been made.
        """
        current: dict[str, CorporateAction] = {}
        for action in (*self._actions, *self.superseded):
            public_at = action.announced_at or action.ex_date
            if public_at > moment:
                continue
            existing = current.get(action.record_id)
            if existing is None or action.revision > existing.revision:
                current[action.record_id] = action
        keep = [a for a in current.values() if a.known_at(moment)]
        return CorporateActionsBook(
            keep,
            provider=self.provider,
            source_sha256=self.source_sha256,
            format_version=self.format_version,
        )

    def public_as_of(self, moment: datetime) -> "CorporateActionsBook":
        """Point-in-time view of what had been published by ``moment`` (any ex-date)."""
        current: dict[str, CorporateAction] = {}
        for action in (*self._actions, *self.superseded):
            if (action.announced_at or action.ex_date) > moment:
                continue
            existing = current.get(action.record_id)
            if existing is None or action.revision > existing.revision:
                current[action.record_id] = action
        return CorporateActionsBook(
            list(current.values()),
            provider=self.provider,
            source_sha256=self.source_sha256,
            format_version=self.format_version,
        )

    def actions_sha256(self) -> str:
        """Hash of the *effective* action set (current revisions, canonical form).

        Two feeds that state the same effective actions hash identically, whatever
        their JSON formatting or superseded history looks like.
        """
        canonical = {
            "format": FORMAT_ID,
            "format_version": self.format_version,
            "provider": self.provider,
            "actions": [a.canonical() for a in self._actions],
        }
        return _canonical_sha256(canonical)

    def history_sha256(self) -> str:
        """Hash of every record received, including superseded revisions."""
        canonical = {
            "format": FORMAT_ID,
            "format_version": self.format_version,
            "provider": self.provider,
            "actions": [
                a.canonical()
                for a in sorted((*self._actions, *self.superseded), key=lambda a: (a.record_id, a.revision))
            ],
        }
        return _canonical_sha256(canonical)


def _canonical_sha256(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


# --------------------------------------------------------------------------- #
# Series building / adjustment
# --------------------------------------------------------------------------- #
@dataclass
class AdjustedSeries:
    """A price series plus the provenance that says what was done to it.

    A ``RAW`` series is the only one that may be handed to paper/execution paths.
    """

    market: str
    symbol: str
    timeframe: str
    bars: list[Candle]
    provenance: dict[str, Any] = field(default_factory=dict)

    @property
    def status(self) -> str:
        return str(self.provenance.get("status") or AdjustmentStatus.UNVERIFIED)

    @property
    def is_raw(self) -> bool:
        return self.status == AdjustmentStatus.RAW

    @property
    def prices_adjusted(self) -> bool:
        return bool(self.provenance.get("prices_adjusted"))

    def for_execution(self) -> list[Candle]:
        """Return the raw candles, or refuse: research prices are not execution prices."""
        if self.prices_adjusted or not self.is_raw:
            raise AdjustedSeriesError(
                f"{self.market}/{self.symbol} series is {self.status}; execution and paper fills "
                "require RAW traded prices, never research-adjusted prices."
            )
        return list(self.bars)


def assert_no_double_counting(provenance: dict[str, Any]) -> None:
    """Refuse a series that would count dividends both in prices and as cash."""
    if bool(provenance.get("dividend_price_adjustment")) and bool(
        provenance.get("dividends_as_cash")
    ):
        raise CorporateActionError(
            "Dividend double counting: a series cannot be dividend-adjusted in prices "
            "and also credit dividend cash."
        )


def _adjust_before(
    bars: list[Candle],
    cutoff: datetime,
    *,
    price_factor: float,
    volume_factor: float,
) -> tuple[list[Candle], int]:
    """Scale OHLC/volume on bars strictly before ``cutoff``; ``Candle`` is immutable."""
    out: list[Candle] = []
    touched = 0
    for bar in bars:
        if bar.timestamp < cutoff:
            out.append(
                replace(
                    bar,
                    open=bar.open * price_factor,
                    high=bar.high * price_factor,
                    low=bar.low * price_factor,
                    close=bar.close * price_factor,
                    volume=bar.volume * volume_factor,
                )
            )
            touched += 1
        else:
            out.append(bar)
    return out, touched


def _split_adjust(bars: list[Candle], action: CorporateAction) -> tuple[list[Candle], int]:
    factor = action.split_factor
    return _adjust_before(bars, action.ex_date, price_factor=1.0 / factor, volume_factor=factor)


def _close_before(bars: list[Candle], cutoff: datetime) -> float | None:
    prev: float | None = None
    for bar in bars:
        if bar.timestamp < cutoff:
            prev = bar.close
        else:
            break
    return prev


def _dividend_adjust(bars: list[Candle], action: CorporateAction) -> tuple[list[Candle], int]:
    """``P_adj = P * (1 - cash / prev_close)`` for bars before the ex-date."""
    prev_close = _close_before(bars, action.ex_date)
    if prev_close is None or prev_close <= 0:
        raise AdjustmentNotDefensible(
            f"No completed bar before the ex-date of dividend {action.record_id!r}; "
            "the adjustment is not defensible"
        )
    cash = float(action.cash_per_share or 0.0)
    factor = 1.0 - (cash / prev_close)
    if factor <= 0:
        raise AdjustmentNotDefensible(
            f"Dividend {action.record_id!r} (cash {cash}) is not smaller than the prior close "
            f"({prev_close}); refusing to invent an adjustment"
        )
    return _adjust_before(bars, action.ex_date, price_factor=factor, volume_factor=1.0)


def build_series(
    bars: Iterable[Candle],
    book: CorporateActionsBook | None,
    *,
    policy: AdjustmentPolicy | str = AdjustmentPolicy.RAW,
    as_of: datetime | None = None,
    market: str | None = None,
    symbol: str | None = None,
    timeframe: str | None = None,
    dataset_sha256: str = "",
) -> AdjustedSeries:
    """Return the raw or research-adjusted series plus its audit provenance.

    ``as_of`` is the simulated decision time: actions announced or effective after it
    are invisible (no look-ahead). When ``as_of`` is ``None`` the series is built with
    every recorded action, which is only correct for retrospective reports.
    """
    policy = AdjustmentPolicy(str(policy))
    raw = [
        Candle(
            timestamp=b.timestamp,
            open=b.open,
            high=b.high,
            low=b.low,
            close=b.close,
            volume=float(b.volume),
            symbol=b.symbol,
        )
        for b in bars
    ]
    mkt = (market or "").strip().upper()
    sym = (symbol or (raw[0].symbol if raw else "")).strip().upper()
    tf = timeframe or ""

    limitations: list[str] = []
    applied: list[dict[str, Any]] = []
    ignored_not_yet_known: list[str] = []
    pending: list[dict[str, Any]] = []

    if book is None:
        limitations.append(
            "NO_CORPORATE_ACTION_DATA: no verified corporate-action feed is attached to this "
            "series; prices and volumes are unverified raw values and any derived return may be "
            "distorted by splits or dividends."
        )
        return AdjustedSeries(
            market=mkt,
            symbol=sym,
            timeframe=tf,
            bars=raw,
            provenance=_provenance(
                policy=AdjustmentPolicy.RAW,
                status=AdjustmentStatus.UNVERIFIED,
                book=None,
                actions_applied=applied,
                actions_ignored=ignored_not_yet_known,
                pending=pending,
                limitations=limitations,
                dataset_sha256=dataset_sha256,
                as_of=as_of,
            ),
        )

    scoped = book.for_instrument(mkt, sym)
    if not mkt:
        markets = {a.market for a in scoped.actions}
        if len(markets) > 1:
            # Same ticker in several markets: never guess which action set applies.
            limitations.append(
                "AMBIGUOUS_MARKET_SCOPE: the action feed holds records for "
                f"{sym or 'this symbol'} in {sorted(markets)}; pass an explicit market. "
                "No action was applied."
            )
            return AdjustedSeries(
                market=mkt,
                symbol=sym,
                timeframe=tf,
                bars=raw,
                provenance=_provenance(
                    policy=AdjustmentPolicy.RAW,
                    status=AdjustmentStatus.UNVERIFIED,
                    book=scoped,
                    actions_applied=applied,
                    actions_ignored=ignored_not_yet_known,
                    pending=pending,
                    limitations=limitations,
                    dataset_sha256=dataset_sha256,
                    as_of=as_of,
                ),
            )
    if as_of is not None:
        effective_ids = {a.record_id for a in scoped.as_of(as_of).actions}
        pending = [
            {
                "record_id": a.record_id,
                "action_type": a.action_type,
                "ex_date": a.ex_date.isoformat(),
                "announced_at": a.announced_at.isoformat() if a.announced_at else None,
            }
            for a in scoped.public_as_of(as_of).actions
            if a.record_id not in effective_ids
        ]
        known = scoped.as_of(as_of)
        ignored_not_yet_known = [
            a.record_id for a in scoped.actions if a.record_id not in {k.record_id for k in known.actions}
        ]
        scoped = known

    unsupported = scoped.unsupported_actions()
    if unsupported:
        limitations.append(
            "UNSUPPORTED_ACTIONS_PRESENT: "
            + ", ".join(f"{a.action_type} {a.ex_date.date()} ({a.record_id})" for a in unsupported)
            + " — recorded but NOT adjusted; returns around these dates are not comparable."
        )

    if policy is AdjustmentPolicy.RAW:
        return AdjustedSeries(
            market=mkt,
            symbol=sym,
            timeframe=tf,
            bars=raw,
            provenance=_provenance(
                policy=policy,
                status=AdjustmentStatus.PARTIAL_UNSUPPORTED
                if unsupported
                else AdjustmentStatus.RAW,
                book=scoped,
                actions_applied=applied,
                actions_ignored=ignored_not_yet_known,
                pending=pending,
                limitations=limitations,
                dataset_sha256=dataset_sha256,
                as_of=as_of,
            ),
        )

    for action in scoped.supported_actions():
        if policy is AdjustmentPolicy.SPLIT_ADJUSTED and action.action_type != SPLIT:
            continue
        try:
            if action.action_type == SPLIT:
                raw, touched = _split_adjust(raw, action)
            else:
                raw, touched = _dividend_adjust(raw, action)
        except AdjustmentNotDefensible as exc:
            limitations.append(f"ADJUSTMENT_NOT_DEFENSIBLE: {exc}")
            continue
        applied.append(
            {
                "record_id": action.record_id,
                "action_type": action.action_type,
                "ex_date": action.ex_date.isoformat(),
                "effective_values": action.effective_values(),
                "bars_adjusted": touched,
            }
        )

    status = POLICY_STATUS[policy]
    if unsupported:
        status = AdjustmentStatus.PARTIAL_UNSUPPORTED
    if any(entry.startswith("ADJUSTMENT_NOT_DEFENSIBLE") for entry in limitations):
        status = AdjustmentStatus.PARTIAL_UNSUPPORTED

    return AdjustedSeries(
        market=mkt,
        symbol=sym,
        timeframe=tf,
        bars=raw,
        provenance=_provenance(
            policy=policy,
            status=status,
            book=scoped,
            actions_applied=applied,
            actions_ignored=ignored_not_yet_known,
            pending=pending,
            limitations=limitations,
            dataset_sha256=dataset_sha256,
            as_of=as_of,
        ),
    )


def _provenance(
    *,
    policy: AdjustmentPolicy,
    status: AdjustmentStatus,
    book: CorporateActionsBook | None,
    actions_applied: list[dict[str, Any]],
    actions_ignored: list[str],
    limitations: list[str],
    pending: list[dict[str, Any]] | None = None,
    dataset_sha256: str,
    as_of: datetime | None,
) -> dict[str, Any]:
    dividends_as_cash = policy in {AdjustmentPolicy.RAW, AdjustmentPolicy.SPLIT_ADJUSTED}
    dividend_price_adjustment = policy is AdjustmentPolicy.TOTAL_RETURN_ADJUSTED
    provenance: dict[str, Any] = {
        "policy": policy.value,
        "status": str(status),
        "prices_adjusted": policy is not AdjustmentPolicy.RAW,
        "dividend_price_adjustment": dividend_price_adjustment,
        "dividends_as_cash": dividends_as_cash,
        "cash_dividend_convention": (
            "PRICE_ADJUSTED_NO_CASH" if dividend_price_adjustment else "RAW_PRICE_PLUS_CASH_EVENT"
        ),
        "volume_adjusted": policy is not AdjustmentPolicy.RAW,
        "format": FORMAT_ID,
        "format_version": book.format_version if book is not None else None,
        "actions_source_sha256": book.source_sha256 if book is not None else "",
        "actions_sha256": book.actions_sha256() if book is not None else "",
        "provider": dict(book.provider) if book is not None else {},
        "actions_total": len(book.actions) if book is not None else 0,
        "actions_applied": actions_applied,
        "adjustments_applied_count": len(actions_applied),
        "actions_excluded_point_in_time": actions_ignored,
        "pending_announced_actions": list(pending or []),
        "unsupported_actions": [
            {
                "record_id": a.record_id,
                "action_type": a.action_type,
                "ex_date": a.ex_date.isoformat(),
            }
            for a in (book.unsupported_actions() if book is not None else ())
        ],
        "supported_action_types": list(SUPPORTED_ACTION_TYPES),
        "unsupported_action_types": list(UNSUPPORTED_ACTION_TYPES),
        "look_ahead_rule": (
            "An action affects a simulated date only when ex_date <= as_of and "
            "(announced_at is None or announced_at <= as_of)."
        ),
        "as_of": as_of.isoformat() if as_of is not None else None,
        "dataset_sha256": dataset_sha256,
        "limitations": list(limitations),
        "cash_dividend_events": (
            [
                {
                    "record_id": a.record_id,
                    "ex_date": a.ex_date.isoformat(),
                    "cash_per_share": float(a.cash_per_share or 0.0),
                    "currency": a.currency,
                    "source": a.source,
                    "source_version": a.source_version,
                }
                for a in (book.supported_actions() if book is not None else ())
                if a.action_type == CASH_DIVIDEND
            ]
            if dividends_as_cash
            else []
        ),
        "verified": not limitations and status is not AdjustmentStatus.UNVERIFIED,
    }
    return provenance


def dividend_cash_events(
    book: CorporateActionsBook,
    *,
    market: str,
    symbol: str,
    as_of: datetime | None = None,
) -> list[dict[str, Any]]:
    """Cash-dividend events for a research cash ledger (raw-price convention only)."""
    scoped = book.for_symbol(market, symbol)
    if as_of is not None:
        scoped = scoped.as_of(as_of)
    return [
        {
            "record_id": a.record_id,
            "ex_date": a.ex_date.isoformat(),
            "cash_per_share": float(a.cash_per_share or 0.0),
            "currency": a.currency,
            "source": a.source,
            "source_version": a.source_version,
        }
        for a in scoped.supported_actions()
        if a.action_type == CASH_DIVIDEND
    ]


def apply_split_to_position(
    quantity: float, average_price: float, action: CorporateAction
) -> tuple[float, float]:
    """Split a simulated holding without changing its value (qty * price constant)."""
    if action.action_type != SPLIT:
        raise CorporateActionError("apply_split_to_position requires a SPLIT action")
    factor = action.split_factor
    return float(quantity) * factor, float(average_price) / factor


def assert_raw_candles(bars: Any, *, context: str = "execution") -> list[Candle]:
    """Guard used by execution/paper boundaries: research-adjusted prices are refused."""
    if isinstance(bars, AdjustedSeries):
        return bars.for_execution()
    return list(bars)


def summarise_book(book: CorporateActionsBook | None, *, market: str = "", symbol: str = "") -> dict:
    """Compact, report-friendly view (used by datasets, manifests and API payloads)."""
    if book is None:
        return {
            "status": AdjustmentStatus.UNVERIFIED.value,
            "actions_sha256": "",
            "actions_source_sha256": "",
            "actions_total": 0,
            "provider": {},
            "unsupported_actions": [],
            "supported_action_types": list(SUPPORTED_ACTION_TYPES),
            "unsupported_action_types": list(UNSUPPORTED_ACTION_TYPES),
            "limitations": ["NO_CORPORATE_ACTION_DATA"],
        }
    scoped = book.for_instrument(market, symbol)
    return {
        "status": AdjustmentStatus.RAW.value,
        "actions_sha256": book.actions_sha256(),
        "actions_source_sha256": book.source_sha256,
        "actions_total": len(scoped.actions),
        "provider": dict(book.provider),
        "unsupported_actions": [
            {"record_id": a.record_id, "action_type": a.action_type, "ex_date": a.ex_date.isoformat()}
            for a in scoped.unsupported_actions()
        ],
        "supported_action_types": list(SUPPORTED_ACTION_TYPES),
        "unsupported_action_types": list(UNSUPPORTED_ACTION_TYPES),
        "limitations": (
            ["UNSUPPORTED_ACTIONS_PRESENT"] if scoped.unsupported_actions() else []
        ),
    }


# --------------------------------------------------------------------------- #
# Dataset / manifest integration helpers
# --------------------------------------------------------------------------- #
def canonical_bars_sha256(bars: Iterable[Candle]) -> str:
    """Deterministic fingerprint of a bar series (prices at 8dp, volume at 4dp)."""
    hasher = hashlib.sha256()
    for b in bars:
        hasher.update(
            (
                f"{b.timestamp.isoformat()}|{b.open:.8f}|{b.high:.8f}|"
                f"{b.low:.8f}|{b.close:.8f}|{float(b.volume):.4f}\n"
            ).encode("utf-8")
        )
    return hasher.hexdigest()


def evaluate_claim(claim: str, status: str) -> tuple[bool, list[str]]:
    """Check a free-text adjustment claim against the verified status.

    A claim is honoured only when verified action data backs every part of it;
    otherwise the record is labelled unverified and the reason is stated.
    """
    text = str(claim or "").strip().upper()
    if not text or "UNVERIFIED" in text or text in {"NOT_APPLICABLE_SYNTHETIC", "RAW", "NONE"}:
        return True, []
    status = str(status)
    adjusted = {AdjustmentStatus.SPLIT_ADJUSTED.value, AdjustmentStatus.DIVIDEND_ADJUSTED.value}
    claims_split = "SPLIT" in text
    claims_dividend = "DIVIDEND" in text or "TOTAL_RETURN" in text or "TOTAL_RETURNS" in text
    generic = "ADJUST" in text and not claims_split and not claims_dividend
    if not (claims_split or claims_dividend or generic):
        return True, []
    problems: list[str] = []
    if claims_split and status not in adjusted:
        problems.append("split adjustment")
    if claims_dividend and status != AdjustmentStatus.DIVIDEND_ADJUSTED.value:
        problems.append("dividend price adjustment")
    if generic and status not in adjusted:
        problems.append("price adjustment")
    if not problems:
        return True, []
    return False, [
        f"UNVERIFIED_ADJUSTMENT_CLAIM: dataset metadata claims {claim!r} but the attached "
        f"corporate-action data does not support it (verified status {status}; missing: "
        f"{', '.join(problems)}); the claim is recorded but NOT honoured."
    ]


def dataset_block(
    bars: list[Candle],
    book: CorporateActionsBook | None,
    *,
    policy: AdjustmentPolicy | str = AdjustmentPolicy.RAW,
    market: str = "",
    symbol: str = "",
    timeframe: str = "",
    dataset_sha256: str = "",
    claim: str = "",
) -> dict[str, Any]:
    """Full provenance block for a dataset/series record (dataset metadata + manifests)."""
    policy = AdjustmentPolicy(str(policy))
    scoped_actions = book.for_instrument(market, symbol).actions if book is not None else ()
    series = build_series(
        bars,
        book,
        policy=policy,
        market=market,
        symbol=symbol,
        timeframe=timeframe,
        dataset_sha256=dataset_sha256 or canonical_bars_sha256(bars),
    )
    block = dict(series.provenance)
    block["dataset_bars_sha256"] = dataset_sha256 or canonical_bars_sha256(bars)
    block["adjusted_bars_sha256"] = canonical_bars_sha256(series.bars)
    block["raw_bars_for_execution"] = True
    block["claim"] = str(claim or "")
    honoured, claim_limitations = evaluate_claim(claim, block["status"])
    block["claim_honoured"] = honoured
    block["limitations"] = [*block["limitations"], *claim_limitations]
    if book is not None and not scoped_actions:
        block["status"] = str(AdjustmentStatus.UNVERIFIED)
        block["limitations"].append(
            "NO_CORPORATE_ACTION_DATA_FOR_INSTRUMENT: the attached feed contains no action for "
            f"{market or '?'}/{symbol or '?'}; the series stays unverified."
        )
    if book is None:
        block["status"] = str(AdjustmentStatus.UNVERIFIED)
    block["verified"] = bool(
        block["status"] != str(AdjustmentStatus.UNVERIFIED)
        and not block["limitations"]
        and honoured
    )
    block["actions_history_sha256"] = book.history_sha256() if book is not None else ""
    block["superseded_records"] = [
        {"record_id": a.record_id, "revision": a.revision, "ex_date": a.ex_date.isoformat()}
        for a in (book.superseded if book is not None else ())
    ]
    block["dividend_accounting"] = (
        "DIVIDENDS_FOLDED_INTO_PRICES_NO_SEPARATE_CASH"
        if block["dividend_price_adjustment"]
        else "RAW_PRICES_PLUS_DIVIDEND_CASH_EVENTS (credit cash once in the research ledger; "
        "never also adjust prices)"
    )
    return block


def report_status(block: dict[str, Any]) -> str:
    """Human-facing status string used in research/ensemble reports."""
    if block.get("verified"):
        return "CORPORATE_ACTIONS_VERIFIED"
    if str(block.get("status")) in {
        AdjustmentStatus.SPLIT_ADJUSTED.value,
        AdjustmentStatus.DIVIDEND_ADJUSTED.value,
    }:
        return "PARTIALLY_VERIFIED_CORPORATE_ACTION_ADJUSTMENT"
    return "UNVERIFIED_CORPORATE_ACTION_ADJUSTMENT"


def block_for_meta(meta: dict[str, Any]) -> dict[str, Any]:
    """Return the corporate-action block for a dataset/run record, never ``None``."""
    block = meta.get("corporate_actions")
    if isinstance(block, dict) and block.get("status"):
        return dict(block)
    claim = str(meta.get("corporate_action_adjustment") or "")
    legacy = summarise_book(None)
    legacy["claim"] = claim
    legacy["policy"] = AdjustmentPolicy.RAW.value
    legacy["prices_adjusted"] = False
    legacy["dividend_price_adjustment"] = False
    legacy["dividends_as_cash"] = True
    legacy["claim_honoured"] = False if "UNVERIFIED" not in claim.upper() else True
    legacy["verified"] = False
    if claim.upper() == "NOT_APPLICABLE_SYNTHETIC":
        legacy["limitations"] = [
            "NOT_APPLICABLE_SYNTHETIC: bundled synthetic fixture — corporate actions are not "
            "applicable, and no adjustment is claimed."
        ]
        legacy["claim_honoured"] = True
    else:
        legacy["limitations"] = [
            "NO_CORPORATE_ACTION_DATA: no verified corporate-action feed is attached to this "
            "dataset; prices/volumes are raw and unverified for splits and dividends."
        ]
    return legacy
