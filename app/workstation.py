"""AXIOM-MONEY-PRINTER workstation composition root.

One factory builds every workstation service against the SAME repository and
settings the rest of the application uses — no disconnected demos. Routes
read these services from ``app.state.workstation``; tests build the same
object directly (no HTTP needed).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.config.settings import Settings
from app.intel.agreement import AgreementLedger, load_votes
from app.intel.analogues import AnalogueEngine
from app.intel.research_loop import ResearchScheduler
from app.intel.evidence import EvidenceStack
from app.intel.missed import ExpiredCandidateRecorder, MissedOpportunityRecorder
from app.intel.opportunity import OpportunityCenter
from app.intel.regime_bridge import RegimeBridge
from app.live.guard_chain import default_chain
from app.live.rate_limit import RateLimiter
from app.live.session import LiveSessionManager
from app.models.studio import KronosStudio
from app.state.bus import EventBus
from app.state.changes import ChangeEngine
from app.system.backup import BackupManager
from app.system.latency import LatencyTracker
from app.system.resources import ResourceGovernor
from app.system.tasks import TaskCenter
from app.system.updater import UpdateManager


@dataclass
class Workstation:
    settings: Settings
    repository: object
    data_dir: Path
    bus: EventBus
    changes: ChangeEngine
    opportunities: OpportunityCenter
    missed: MissedOpportunityRecorder
    expired_recorder: ExpiredCandidateRecorder
    latency: LatencyTracker
    governor: ResourceGovernor
    tasks: TaskCenter
    live_sessions: LiveSessionManager
    rate_limiter: RateLimiter
    studio: KronosStudio
    backup: BackupManager
    analogues: AnalogueEngine
    regime_bridge: RegimeBridge
    updater: UpdateManager
    agreement: "AgreementLedger"
    research_scheduler: "ResearchScheduler"

    def guard_chain(self, *, emergency_stop: bool = False,
                    recent_intent_ids: set | None = None):
        """A guard chain bound to current live-session state."""
        state = self.live_sessions.current().state
        return default_chain(settings=self.settings, session_state=state,
                             emergency_stop=emergency_stop or self.emergency_stop_active(),
                             recent_intent_ids=recent_intent_ids)

    def emergency_stop_active(self) -> bool:
        from app.live.ops import read_emergency_stop

        try:
            active, _ = read_emergency_stop(self.repository)
        except Exception:
            # Unreadable safety flag must fail closed.
            return True
        return active

    def evidence_stack(self, market_id: str, identity: str) -> EvidenceStack:
        return EvidenceStack(identity=identity, market_id=market_id)

    def attention_mode(self) -> str:
        try:
            stored = self.repository.get_engine_state("attention_mode", None)
        except Exception:
            stored = None
        return stored or getattr(self.settings, "attention_mode", "balanced")


def build(settings: Settings, repository, data_dir: str | Path) -> Workstation:
    data_dir = Path(data_dir)
    bus = EventBus()
    changes = ChangeEngine(repository, bus=bus)
    attention = None
    try:
        attention = repository.get_engine_state("attention_mode", None) \
            or getattr(settings, "attention_mode", "balanced")
    except Exception:
        attention = getattr(settings, "attention_mode", "balanced")
    opportunities = OpportunityCenter(repository, bus=bus,
                                      attention_mode=attention or "balanced")
    missed = MissedOpportunityRecorder(repository)
    expired = ExpiredCandidateRecorder(missed)
    latency = LatencyTracker()
    governor = ResourceGovernor(disk_path=str(data_dir))
    tasks = TaskCenter(repository, governor=governor)
    live_sessions = LiveSessionManager(repository, bus=bus)
    profile = getattr(settings, "live_rate_limit_profile", "REGULAR_ALGO") or "REGULAR_ALGO"
    try:
        rate_limiter = RateLimiter(profile)
    except KeyError:
        rate_limiter = RateLimiter("REGULAR_ALGO")
    try:
        from axiom.kronos.model_cache import ModelCache

        studio = KronosStudio(ModelCache(), repository=repository)
    except Exception:
        studio = KronosStudio(repository=repository)
    backup = BackupManager(settings.database_path() if hasattr(settings, "database_path")
                           else _db_path(settings), backup_dir=data_dir / "backups")
    market_id = str(getattr(settings, "market", "india") or "india")
    regime_bridge = RegimeBridge(changes, opportunities, market_id=market_id)
    import sys as _sys

    from app import __version__ as _version
    updater = UpdateManager(
        data_dir / "updates", current_version=_version,
        target_path=Path(_sys.executable) if getattr(_sys, "frozen", False) else None)
    return Workstation(
        settings=settings, repository=repository, data_dir=data_dir, bus=bus,
        changes=changes, opportunities=opportunities, missed=missed,
        expired_recorder=expired, latency=latency, governor=governor, tasks=tasks,
        live_sessions=live_sessions, rate_limiter=rate_limiter, studio=studio,
        backup=backup, analogues=AnalogueEngine(), regime_bridge=regime_bridge,
        updater=updater, agreement=_agreement_ledger(data_dir),
        research_scheduler=ResearchScheduler(
            governor=governor,
            live_active=lambda: live_sessions.current().state in
            ("ARMED", "ACTIVE", "RECONCILING"),
            market_open=_pinned_market_open(repository)))


def _pinned_market_open(repository):
    """Session awareness for the research scheduler (§97): the market of the
    pinned universe decides the window. No pin -> None (unknown, reported
    honestly — never a guessed open/closed)."""
    def probe() -> bool | None:
        try:
            from app.markets import list_markets, market_session_state

            for market in list_markets():
                market_id = market["market_id"]
                if repository is not None and repository.list_pinned(market_id):
                    state = market_session_state(market_id)
                    return state.status in ("OPEN", "ALWAYS")
        except Exception:
            return None
        return None
    return probe


def _agreement_ledger(data_dir: Path) -> AgreementLedger:
    """Agreement votes persist across restarts (§29); a missing or damaged
    file simply starts the ledger empty."""
    ledger = AgreementLedger()
    load_votes(Path(data_dir) / "agreement_votes.json", ledger)
    return ledger


def _db_path(settings: Settings) -> Path:
    url = getattr(settings, "database_url", "") or ""
    if url.startswith("sqlite:///"):
        return Path(url[len("sqlite:///"):])
    return Path("trading_bot.db")
