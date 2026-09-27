"""Arena 3.0.4 Market Event Listener — 20 deterministic tests (offline, fixtures only).

Validates: relevant/irrelevant/duplicate/conflicting/low-confidence/timeout/failure/malformed/
impact/NO_TRADE→Router/no-direct-order/no-bypass-risk/no-modify-limits/no-enable-LIVE/
persistence/restart-recovery/market-response/dashboard/no-secret-leakage.

All tests use injectable fetcher / tmp_path DB; no live websites, no network.
"""

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config.settings import Settings
from app.database.database import Database
from app.database.repository import Repository
from app.market_events.collector import EventCollector
from app.market_events.detector import is_relevant, validate_source, normalize_event_type
from app.market_events.impact import assess, decide_arena_response, TRANSMISSION
from app.market_events.listener import MarketEventListener
from app.market_events.models import MarketEvent, event_fingerprint, ARENA_RESPONSES
from app.api.server import create_app


# ---------------------------------------------------------------- helpers

def _repo(tmp_path: Path) -> Repository:
    db = Database(f"sqlite:///{tmp_path}/test.db")
    repo = Repository(db)
    return repo

def _make_listener(tmp_path: Path, *, fetcher=None, strategy_source=None):
    repo = _repo(tmp_path)
    collector = EventCollector(fetcher=fetcher, timeout=0.5, max_retries=1, backoff_base=0.01, cache_ttl=10)
    listener = MarketEventListener(repo, collector=collector, strategy_source=strategy_source)
    return repo, listener

# a qualified strategy source fixture
class _QualifiedStrategy:
    def select(self, regime=None):
        return {"selected": "momentum", "executable": True, "parameters": {"period": 14}}, None

class _UnqualifiedStrategy:
    def select(self, regime=None):
        return {"selected": None, "executable": False}, None

# ---------------------------------------------------------------- 1 relevant

def test_1_relevant_war_detected_not_ignore(tmp_path):
    repo, listener = _make_listener(tmp_path)
    raw = {
        "headline": "War escalation: military offensive triggers oil supply disruption, sanctions imposed",
        "summary": "Geopolitical tension rises as central bank holds rates, energy sector on alert",
        "timestamp": "2026-09-26T00:00:00+00:00",
    }
    ev = listener.ingest_raw_article(raw, source="reuters", source_url="https://reuters.com/world/war-oil-1")
    assert ev is not None
    assert ev.event_type in {"war", "military_escalation", "oil_supply", "sanctions"}
    # relevant war must NOT be IGNORE; must be MONITOR/EVALUATE/NO_TRADE (NO_TRADE when no strategy)
    assert ev.arena_response in ARENA_RESPONSES
    assert ev.arena_response != "IGNORE"
    assert ev.relevance > 0.3
    assert ev.confidence > 0.2
    # persisted
    fetched = repo.get_market_event(ev.event_id)
    assert fetched is not None
    assert fetched["headline"] == ev.headline
    repo.db.close()

# ---------------------------------------------------------------- 2 irrelevant

def test_2_irrelevant_celebrity_filtered_ignore(tmp_path):
    repo, listener = _make_listener(tmp_path)
    raw = {
        "headline": "Celebrity gossip: sports star launches new perfume",
        "summary": "Entertainment news and local weather recipe horoscope",
    }
    # detector directly
    relevant, score, reason = is_relevant(raw["headline"], raw["summary"])
    assert relevant is False
    assert score < 0.3
    # via listener — should be ingested but marked IGNORE (or NONE)
    ev = listener.ingest_raw_article(raw, source="gossip-site", source_url="https://gossip.example.com/story")
    assert ev is not None
    assert ev.arena_response == "IGNORE"
    assert ev.relevance < 0.3
    repo.db.close()

# ---------------------------------------------------------------- 3 duplicate

def test_3_duplicate_event_deduplicated_to_one(tmp_path):
    repo, listener = _make_listener(tmp_path)
    raw = {"headline": "US-China tariffs escalate, trade war hits technology sector", "summary": "Tariff announcement weighs on equities and FX"}
    ev1 = listener.ingest_raw_article(raw, source="reuters", source_url="https://reuters.com/tariff-1")
    assert ev1 is not None
    count1 = len(repo.list_market_events(limit=100))
    # same headline/type/countries → same fingerprint, should dedup
    ev2 = listener.ingest_raw_article(raw, source="bloomberg", source_url="https://bloomberg.com/tariff-1b")
    assert ev2 is not None
    # second ingest returns existing event (merged)
    assert ev2.event_id == ev1.event_id or ev2.fingerprint == ev1.fingerprint
    count2 = len(repo.list_market_events(limit=100))
    assert count2 == count1  # no new row
    # source merged
    fetched = repo.get_market_event(ev1.event_id)
    sources = fetched.get("sources") or []
    assert len(sources) >= 2 or len(repo.list_market_event_sources(ev1.event_id)) >= 2
    repo.db.close()

# ---------------------------------------------------------------- 4 conflicting

def test_4_conflicting_sources_reduce_confidence(tmp_path):
    repo, listener = _make_listener(tmp_path)
    raw1 = {"headline": "OPEC oil supply cut agreed, crude prices jump", "summary": "Summary version A: major cut of 1M barrels"}
    raw2 = {"headline": "OPEC oil supply cut agreed, crude prices jump", "summary": "Summary version B: conflicting report says no cut"}
    ev1 = listener.ingest_raw_article(raw1, source="reuters", source_url="https://reuters.com/opec-1")
    conf_before = ev1.confidence
    ev2 = listener.ingest_raw_article(raw2, source="bloomberg", source_url="https://bloomberg.com/opec-1b")
    assert ev2 is not None
    fetched = repo.get_market_event(ev1.event_id)
    # conflicting sources should have reduced confidence and conflict_note
    assert fetched["confidence"] < conf_before or fetched.get("conflict_note") is not None
    if fetched.get("conflict_note"):
        assert "conflict" in fetched["conflict_note"].lower()
    repo.db.close()

# ---------------------------------------------------------------- 5 low-confidence

def test_5_low_confidence_results_in_monitor_or_no_trade(tmp_path):
    repo, listener = _make_listener(tmp_path)
    raw = {"headline": "War escalation triggers sanctions, oil supply at risk", "summary": "Markets await central bank decision"}
    # fake source → low confidence
    ev = listener.ingest_raw_article(raw, source="fake-news", source_url="https://example.com/fake-war")
    assert ev is not None
    assert ev.confidence < 0.5  # fake marker penalty
    # low confidence must NOT be EVALUATE; should be MONITOR or NO_TRADE
    assert ev.arena_response in {"MONITOR", "NO_TRADE", "IGNORE"}
    repo.db.close()

# ---------------------------------------------------------------- 6 timeout isolated

def test_6_timeout_isolated_arena_continues(tmp_path):
    # collector with fetcher that simulates timeout via exception
    def timeout_fetcher(url):
        raise TimeoutError("simulated timeout")
    repo, listener = _make_listener(tmp_path, fetcher=timeout_fetcher)
    # ingest_from_sources should isolate failure, return empty, not raise
    events = listener.ingest_from_sources([("reuters", "https://reuters.com/timeout")])
    assert events == []
    # direct collector fetch should return ok=False, not raise
    coll = EventCollector(fetcher=timeout_fetcher, timeout=0.1, max_retries=0, backoff_base=0.01)
    res = coll.fetch("https://reuters.com/timeout", source="reuters")
    assert res["ok"] is False
    assert "timeout" in res["error"].lower() or "Timeout" in res["error"]
    # Arena continues — we can still ingest a valid article afterward
    raw = {"headline": "Central bank holds interest rates steady, policy decision", "summary": "Policy decision: federal reserve signals hold"}
    ev = listener.ingest_raw_article(raw, source="reuters", source_url="https://reuters.com/policy-1")
    assert ev is not None
    repo.db.close()

# ---------------------------------------------------------------- 7 failure isolation

def test_7_fetch_failure_isolated_other_sources_still_processed(tmp_path):
    def selective_fetcher(url):
        if "fail" in url:
            raise ConnectionError("connection refused")
        return {"headline": "Shipping disruption blocks Suez, supply chain woes", "summary": "Shipping disruption widens, inflation impact"}
    repo, listener = _make_listener(tmp_path, fetcher=selective_fetcher)
    events = listener.ingest_from_sources([
        ("reuters", "https://reuters.com/fail-shipping"),
        ("reuters", "https://reuters.com/good-shipping"),
    ])
    # one failed, one succeeded → exactly 1 event ingested, failure isolated
    assert len(events) == 1
    assert "shipping" in events[0].headline.lower() or events[0].event_type == "shipping_disruption"
    repo.db.close()

# ---------------------------------------------------------------- 8 malformed

def test_8_malformed_article_missing_headline_handled(tmp_path):
    repo, listener = _make_listener(tmp_path)
    # missing headline → ingest returns None, does not crash, no row
    bad = {"summary": "Tariffs without headline", "content": "empty headline"}
    ev = listener.ingest_raw_article(bad, source="reuters", source_url="https://reuters.com/bad")
    assert ev is None
    assert len(repo.list_market_events(limit=100)) == 0
    # empty dict → None
    assert listener.ingest_raw_article({}, source="reuters", source_url="https://reuters.com/empty") is None
    # safe_parse must not eval/exec
    coll = EventCollector()
    assert "error" in coll.safe_parse({"eval": "1+1"})
    assert "error" in coll.safe_parse({"__exec__": "x"})
    assert coll.safe_parse({"headline": "ok"}) == {"headline": "ok"}
    repo.db.close()

# ---------------------------------------------------------------- 9 impact oil

def test_9_impact_oil_supply_transmission_paths(tmp_path):
    repo, listener = _make_listener(tmp_path)
    raw = {"headline": "OPEC oil supply cut sparks crude rally", "summary": "Oil supply constrained, energy and inflation-sensitive assets react"}
    ev = listener.ingest_raw_article(raw, source="reuters", source_url="https://reuters.com/oil-impact")
    assert ev is not None
    assert ev.event_type == "oil_supply"
    # transmission paths must be populated, not simplistic
    assert any("crude" in p.lower() for p in ev.transmission_paths)
    assert "energy" in ev.affected_sectors or "energy" in ev.affected_markets or "commodities" in ev.affected_markets
    assert "crude" in ev.expected_market_effect.lower() or "energy" in ev.expected_market_effect.lower()
    # also direct assess test
    m = MarketEvent(headline="Oil supply shock", summary="crude", event_type="oil_supply", confidence=0.8, relevance=0.8)
    assess(m)
    assert TRANSMISSION["oil_supply"]["paths"][0] in m.transmission_paths
    repo.db.close()

# ---------------------------------------------------------------- 10 NO_TRADE→Router (and check Router context)

def test_10_no_trade_when_no_qualified_strategy_and_router_context(tmp_path):
    # without qualified strategy → NO_TRADE (even for relevant)
    repo, listener = _make_listener(tmp_path, strategy_source=_UnqualifiedStrategy())
    raw = {"headline": "War escalation: sanctions and tariffs hit markets", "summary": "Geopolitical risk premium rises"}
    ev = listener.ingest_raw_article(raw, source="reuters", source_url="https://reuters.com/router-1")
    assert ev is not None
    assert ev.arena_response == "NO_TRADE"
    assert ev.action_taken == "NO_TRADE"
    # with qualified strategy → should be EVALUATE or MONITOR, not IGNORE
    repo2, listener2 = _make_listener(tmp_path, strategy_source=_QualifiedStrategy())
    # use different headline to avoid dedup
    raw2 = {"headline": "War escalation: ceasefire talks collapse, oil at risk", "summary": "Markets weigh de-escalation failure"}
    # ensure fingerprint not seen before — new tmp subpath? use distinct tmp
    import tempfile
    import pathlib as _pl
    p2 = Path(tempfile.mkdtemp())
    repo2b, listener2b = _make_listener(p2, strategy_source=_QualifiedStrategy())
    ev2 = listener2b.ingest_raw_article(raw2, source="reuters", source_url="https://reuters.com/router-2")
    assert ev2 is not None
    assert ev2.arena_response in {"EVALUATE", "MONITOR", "NO_TRADE"}
    # if EVALUATE, verify strategy_used set and no order placed (order not in repo)
    if ev2.arena_response == "EVALUATE":
        assert ev2.action_taken == "ROUTER_CONSULTED"
        assert len(repo2b.list_sandbox_orders(limit=10)) == 0  # no order bypass
    repo.db.close()
    repo2.db.close()
    repo2b.db.close()

# ---------------------------------------------------------------- 11 no direct order

def test_11_listener_never_directly_submits_orders(tmp_path):
    repo, listener = _make_listener(tmp_path)
    # forbidden methods must raise
    with pytest.raises(RuntimeError, match="must never directly place orders"):
        listener.submit_order(side="BUY", quantity=1)
    with pytest.raises(RuntimeError, match="must never directly place orders"):
        listener.place_order(symbol="NSE_EQ|INE002A01018")
    # ingest must not create any orders in repository
    raw = {"headline": "Tariffs announced on China tech imports, trade war deepens", "summary": "Trade restriction impacts technology and FX"}
    ev = listener.ingest_raw_article(raw, source="reuters", source_url="https://reuters.com/notrade-1")
    assert ev is not None
    # no orders created via any pipeline
    assert len(repo.list_sandbox_orders(limit=100)) == 0
    # listener class should not contain broker submit calls
    import inspect
    src = inspect.getsource(listener.__class__)
    assert "broker.submit" not in src
    assert "broker.place" not in src
    repo.db.close()

# ---------------------------------------------------------------- 12 no bypass risk

def test_12_listener_does_not_bypass_emergency_stop_risk(tmp_path):
    repo, listener = _make_listener(tmp_path)
    # set emergency STOP — hard risk gate
    repo.set_engine_state("emergency_stop", {"stopped": True, "reason": "test"})
    # need fresh listener to see emergency (or same reads at ingest time)
    raw = {"headline": "Central bank emergency policy decision, war risk elevated", "summary": "Policy decision under geopolitical stress"}
    ev = listener.ingest_raw_article(raw, source="reuters", source_url="https://reuters.com/risk-1")
    assert ev is not None
    assert ev.arena_response == "NO_TRADE"
    assert ev.action_taken == "NO_TRADE"
    assert "emergency" in json.dumps(ev.result_after_event or {}).lower() or "STOP" in json.dumps(ev.result_after_event or {})
    # verify listener checks emergency_stop via repository (not bypass)
    import inspect
    src = inspect.getsource(listener.ingest_raw_article)
    assert "emergency_stop" in src
    repo.db.close()

# ---------------------------------------------------------------- 13 no modify limits

def test_13_listener_cannot_modify_risk_limits(tmp_path):
    repo, listener = _make_listener(tmp_path)
    with pytest.raises(RuntimeError, match="cannot modify risk limits"):
        listener.modify_risk_limits(max_position=9999)
    # ingest must not alter risk limits (stored via bankroll config)
    # check that no risk keys were changed
    import inspect
    src = inspect.getsource(listener.__class__)
    assert "risk_limits" not in src.lower() or "cannot modify" in src.lower()
    # ensure no method to change drawdown etc
    assert not hasattr(listener, "set_risk")
    assert not hasattr(listener, "update_bankroll")
    repo.db.close()

# ---------------------------------------------------------------- 14 no enable LIVE

def test_14_listener_cannot_enable_live_trading(tmp_path):
    repo, listener = _make_listener(tmp_path)
    with pytest.raises(RuntimeError, match="cannot enable LIVE"):
        listener.enable_live()
    # verify settings still PAPER after attempts
    from app.config.settings import Settings
    s = Settings.from_env()
    assert s.live_trading is False
    assert s.mode == "PAPER"
    # also check that listener never mutates LIVE_TRADING
    import inspect
    src = inspect.getsource(listener.__class__)
    assert "LIVE_TRADING" not in src or "cannot enable" in src
    repo.db.close()

# ---------------------------------------------------------------- 15 persistence

def test_15_persistence_get_list_update(tmp_path):
    repo, listener = _make_listener(tmp_path)
    raw = {"headline": "Sanctions relief announced, energy markets react", "summary": "Sanctions relief eases oil flows, commodities adjust"}
    ev = listener.ingest_raw_article(raw, source="reuters", source_url="https://reuters.com/persist-1")
    assert ev is not None
    # get
    fetched = repo.get_market_event(ev.event_id)
    assert fetched is not None
    assert fetched["event_id"] == ev.event_id
    assert fetched["fingerprint"] == ev.fingerprint
    # list
    lst = repo.list_market_events(limit=10)
    assert any(x["event_id"] == ev.event_id for x in lst)
    # update
    repo.update_market_event(ev.event_id, arena_response="MONITOR")
    assert repo.get_market_event(ev.event_id)["arena_response"] == "MONITOR"
    # sources
    repo.add_market_event_source(ev.event_id, __import__("app.market_events.models", fromlist=["MarketEventSource"]).MarketEventSource(source="ap", source_url="https://apnews.com/persist-1"))
    assert len(repo.list_market_event_sources(ev.event_id)) >= 2
    repo.db.close()

# ---------------------------------------------------------------- 16 restart recovery

def test_16_restart_recovery_events_survive_db_reopen(tmp_path):
    db_path = tmp_path / "persist.db"
    db = Database(f"sqlite:///{db_path}")
    repo = Repository(db)
    collector = EventCollector(fetcher=lambda u: {"headline": "Shipping disruption widens, gas supply hit", "summary": "Shipping and gas"})
    listener = MarketEventListener(repo, collector=collector)
    raw = {"headline": "Shipping disruption widens, gas supply hit", "summary": "Logistics costs rise, gas supply crimped"}
    ev = listener.ingest_raw_article(raw, source="reuters", source_url="https://reuters.com/restart-1")
    eid = ev.event_id
    fp = ev.fingerprint
    db.close()
    # reopen (simulating restart) — must recover without loss
    db2 = Database(f"sqlite:///{db_path}")
    repo2 = Repository(db2)
    assert repo2.get_market_event(eid) is not None
    assert repo2.get_market_event(eid)["headline"] == ev.headline
    assert repo2.get_market_event_by_fingerprint(fp) is not None
    assert len(repo2.list_market_events(limit=10)) >= 1
    # new listener loads fingerprint cache from DB
    listener2 = MarketEventListener(repo2, collector=collector)
    assert fp in listener2._seen_fingerprints
    db2.close()

# ---------------------------------------------------------------- 17 market-response

def test_17_record_market_response_six_windows(tmp_path):
    repo, listener = _make_listener(tmp_path)
    # need some snapshots for movement calculation
    from datetime import datetime, timezone
    repo.upsert_daily_snapshot("2026-09-25", equity=100000, realized=0, unrealized=0, trades=5, regime=None)
    repo.upsert_daily_snapshot("2026-09-26", equity=100500, realized=500, unrealized=0, trades=6, regime=None)
    raw = {"headline": "Regulatory crackdown on finance, policy decision", "summary": "Regulatory shift moves financials"}
    ev = listener.ingest_raw_article(raw, source="reuters", source_url="https://reuters.com/obs-1")
    assert ev is not None
    summary = listener.record_market_response(ev.event_id)
    assert summary is not None
    assert "observations" in summary
    assert len(summary["observations"]) == 6
    windows = {o["window"] for o in summary["observations"]}
    assert windows == {"5m", "30m", "1h", "1d", "3d", "5d"}
    # persisted in observations table
    obs = repo.list_market_observations(ev.event_id)
    assert len(obs) == 6
    # also updated event's result_after_event
    fetched = repo.get_market_event(ev.event_id)
    assert fetched["result_after_event"] is not None
    assert "observations" in fetched["result_after_event"]
    repo.db.close()

# ---------------------------------------------------------------- 18 dashboard

def test_18_dashboard_includes_market_events_and_escapes_html(tmp_path):
    db_path = tmp_path / "dash.db"
    settings = Settings(api_token="dash-secret-123", database_url=f"sqlite:///{db_path}", data_source="upstox")
    app = create_app(settings, data_dir=tmp_path)
    # create event before request (needs DB tables via lifespan? we create via repository directly)
    from app.database.database import Database as DB2
    from app.database.repository import Repository as Repo2
    # ensure DB initialized via app lifespan: use TestClient which runs lifespan
    with TestClient(app) as client:
        # insert via API (ingest)
        # we need to inject a fake fetcher? but we can use direct /api/market-events/ingest
        raw = {"headline": "<script>alert(1)</script> War oil supply at risk", "summary": "Summary with <b>html</b>"}
        # ingest via API with bearer
        h = {"Authorization": "Bearer dash-secret-123"}
        r = client.post("/api/market-events/ingest", json={"headline": raw["headline"], "summary": raw["summary"], "source": "reuters", "source_url": "https://reuters.com/dash-1"}, headers=h)
        assert r.status_code == 200, r.text
        # dashboard data must include market_events, sanitized
        dr = client.get("/dashboard/data", headers=h).json()
        assert "market_events" in dr
        assert len(dr["market_events"]) >= 1
        # headline must be escaped / stripped of < > (our listener strips)
        headlines = [e["headline"] for e in dr["market_events"]]
        assert not any("<script>" in h2 for h2 in headlines)
        assert not any("<b>" in h2 for h2 in headlines)
        # HTML page must not contain raw script tag from headline (no raw HTML render)
        html = client.get("/dashboard", headers=h).text
        assert "<script>alert(1)</script>" not in html
        # market events panel exists
        assert "Market Events" in html
        assert 'id="market_events"' in html

# ---------------------------------------------------------------- 19 no secret leakage

def test_19_no_secret_leakage_in_dashboard_and_events(tmp_path):
    settings = Settings(api_token="super-secret-token-xyz", database_url=f"sqlite:///{tmp_path}/leak.db")
    app = create_app(settings, data_dir=tmp_path)
    with TestClient(app) as client:
        h = {"Authorization": "Bearer super-secret-token-xyz"}
        # ingest an event
        client.post("/api/market-events/ingest", json={"headline": "War triggers oil supply cut", "summary": "test", "source": "reuters", "source_url": "https://reuters.com/leak-1"}, headers=h)
        data = client.get("/dashboard/data", headers=h).json()
        dump = json.dumps(data)
        assert "super-secret-token-xyz" not in dump
        assert "super-secret-token-xyz" not in client.get("/dashboard", headers=h).text
        # market events list must not contain token
        me = client.get("/api/market-events", headers=h).json()
        assert "super-secret-token-xyz" not in json.dumps(me)
        # token in query param must be rejected (no leak via URL)
        r = client.get("/dashboard/data?token=super-secret-token-xyz")
        assert r.status_code == 401
        assert "super-secret-token-xyz" not in r.text

# ---------------------------------------------------------------- 20 additional: data-collection safeguards

def test_20_data_collection_rate_limit_cache_isolation_and_no_eval(tmp_path):
    # rate limit
    coll = EventCollector(fetcher=lambda u: {"headline": "test tariff", "summary": "tariff"}, rate_limit_per_minute=2, cache_ttl=60, backoff_base=0.01)
    # 2 fetches ok, 3rd rate-limited
    r1 = coll.fetch("https://reuters.com/a", source="reuters")
    r2 = coll.fetch("https://reuters.com/b", source="reuters")
    r3 = coll.fetch("https://reuters.com/c", source="reuters")
    assert r1["ok"] is True and r2["ok"] is True
    assert r3["ok"] is False and r3["error"] == "rate_limited"
    # cache: second fetch of same URL should be cached even after fetcher changed to failing
    coll2 = EventCollector(fetcher=lambda u: {"headline": "cached", "summary": "cached"}, cache_ttl=60, backoff_base=0.01)
    coll2.fetch("https://reuters.com/cached", source="reuters")
    coll2.fetcher = lambda u: (_ for _ in ()).throw(RuntimeError("should not be called"))
    r_cached = coll2.fetch("https://reuters.com/cached", source="reuters")
    assert r_cached.get("cached") is True
    # isolation: fetch_many continues after one fails
    coll3 = EventCollector(fetcher=lambda u: (_ for _ in ()).throw(ValueError("boom")) if "boom" in u else {"headline": "ok", "summary": "ok"}, max_retries=0, backoff_base=0.01)
    res = coll3.fetch_many(["https://a.com/boom", "https://a.com/ok"], source="test")
    assert res[0]["ok"] is False and res[1]["ok"] is True
    # no eval/exec on downloaded content
    assert "error" in coll.safe_parse({"eval": "import os"})
    assert "error" in coll.safe_parse({"exec": "print(1)"})
