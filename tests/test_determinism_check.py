"""Tests for tools/determinism_check.py — including that it can FAIL.

A determinism check that always passes is worse than none, so the important
tests here are the ones that inject non-determinism and require the tool to
report it, plus the false-positive guards (opaque uuid identities must not count
as a difference).
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import determinism_check as dc  # noqa: E402


class TestFingerprintCanonicalisation:
    def test_opaque_identities_are_relabelled_not_compared(self):
        """Two runs mint different uuid4s; that alone must not be a difference."""
        left = {"fills": [{"order_id": "aaaa1111", "price": 1.0},
                          {"order_id": "bbbb2222", "price": 2.0}]}
        right = {"fills": [{"order_id": "cccc3333", "price": 1.0},
                           {"order_id": "dddd4444", "price": 2.0}]}
        assert dc._canonical(left) == dc._canonical(right)

    def test_reference_structure_is_preserved(self):
        """The same order appearing twice must stay distinguishable from two."""
        shared = {"fills": [{"order_id": "x", "price": 1.0},
                            {"order_id": "x", "price": 2.0}]}
        distinct = {"fills": [{"order_id": "x", "price": 1.0},
                              {"order_id": "y", "price": 2.0}]}
        assert dc._canonical(shared) != dc._canonical(distinct)

    def test_wall_clock_keys_and_float_noise_are_dropped(self):
        left = {"created_at": "2024-01-01T00:00:00+00:00", "pnl": 1.0000000001}
        right = {"created_at": "2024-06-01T00:00:00+00:00", "pnl": 1.0000000002}
        assert dc._canonical(left) == dc._canonical(right)

    def test_a_real_decision_difference_survives_canonicalisation(self):
        left = {"trades": [{"entry": 100.0, "quantity": 10, "pnl": 5.0}]}
        right = {"trades": [{"entry": 100.0, "quantity": 11, "pnl": 5.0}]}
        assert dc._canonical(left) != dc._canonical(right)

    def test_the_synthetic_market_is_identical_between_calls(self):
        first = dc.synthetic_candles(50)
        second = dc.synthetic_candles(50)
        assert [(c.timestamp, c.open, c.high, c.low, c.close, c.volume)
                for c in first] == [(c.timestamp, c.open, c.high, c.low, c.close,
                                     c.volume) for c in second]


class TestFirstDifference:
    def test_it_names_the_location_of_a_mismatch(self):
        text = dc._first_difference({"a": {"b": [1, 2]}}, {"a": {"b": [1, 3]}})
        assert text == ".a.b[1]: 2 vs 3"

    def test_it_reports_missing_and_extra_keys(self):
        assert "missing on the right" in dc._first_difference({"k": 1}, {})
        assert "missing on the left" in dc._first_difference({}, {"k": 1})

    def test_it_reports_a_length_change(self):
        assert dc._first_difference({"k": [1, 2]}, {"k": [1]}) == ".k: 2 vs 1 items"

    def test_identical_values_have_no_difference(self):
        assert dc._first_difference({"a": 1}, {"a": 1}) == ""


class TestDetectsNonDeterminism:
    def test_a_stable_run_passes_and_reports_a_fingerprint(self):
        report = dc.check(candles=60, seeds=())
        assert report["ok"] is True
        assert report["in_process"]["ok"] is True
        assert len(report["fingerprint"]) == 64
        assert report["trades"] > 0, "the scenario must exercise order decisions"
        # an in-process-only run is honest about being narrower
        assert report["cross_process_checked"] is False
        assert dc.check(candles=60, seeds=("0",))["cross_process_checked"] is True

    def test_injected_nondeterminism_is_detected(self, monkeypatch):
        """The whole point: a run that varies must be reported as varying."""
        import random

        original = dc.fingerprint

        def wobbling(result):
            # perturb the actual run result, the way a hash-order dependency or
            # an unseeded random would
            if result.trades:
                result.trades[0]["quantity"] += random.random()
            return original(result)

        monkeypatch.setattr(dc, "fingerprint", wobbling)
        report = dc.check(candles=60, seeds=())
        assert report["ok"] is False
        assert report["in_process"]["ok"] is False
        assert ".trades[0].quantity" in report["in_process"]["difference"]

    def test_a_differing_subprocess_is_reported(self, monkeypatch):
        monkeypatch.setattr(dc, "_subprocess_fingerprint", lambda seed, **kw: {
            "seed": seed, "ok": True, "sha256": "0" * 64, "matches": False})
        report = dc.check(candles=60, seeds=("0",))
        assert report["ok"] is False
        assert report["processes"][0]["matches"] is False

    def test_a_crashing_subprocess_is_reported_not_swallowed(self, monkeypatch):
        monkeypatch.setattr(dc, "_subprocess_fingerprint", lambda seed, **kw: {
            "seed": seed, "ok": False, "error": "boom", "matches": False})
        report = dc.check(candles=60, seeds=("0",))
        assert report["ok"] is False
        assert report["processes"][0]["error"] == "boom"

    def test_the_scenario_without_trades_is_declared_insufficient(self):
        """Silence must not be mistaken for a pass."""
        report = dc.check(candles=3, seeds=())
        if report["trades"] == 0:
            assert "no trades" in report["note"]
        else:  # pragma: no cover - the scenario changed; keep the invariant
            assert report["trades"] > 0 and "note" not in report


class TestCrossProcess:
    def test_a_fresh_process_returns_a_bare_fingerprint(self):
        outcome = dc._subprocess_fingerprint("0", candles=60)
        assert outcome["ok"] is True
        sha = outcome["sha256"]
        assert len(sha) == 64 and '"' not in sha, f"quoting bug: {sha!r}"
        assert all(character in "0123456789abcdef" for character in sha)

    def test_different_hash_seeds_agree(self):
        report = dc.check(candles=60, seeds=("0", "1"))
        assert report["ok"] is True
        assert [item["matches"] for item in report["processes"]] == [True, True]


class TestCli:
    def _run(self, *args):
        return subprocess.run(
            [sys.executable, str(ROOT / "tools" / "determinism_check.py"), *args],
            capture_output=True, text=True, timeout=600, cwd=str(ROOT))

    def test_it_exits_zero_and_prints_json_on_success(self):
        completed = self._run("--candles", "60", "--seeds", "0")
        assert completed.returncode == 0, completed.stderr[-500:]
        report = json.loads(completed.stdout)
        assert report["ok"] is True and report["fingerprint"]

    def test_it_exits_one_when_the_check_fails(self, monkeypatch):
        """A failed check must be a non-zero exit, or CI would never notice."""
        monkeypatch.setattr(dc, "check", lambda **kwargs: {
            "ok": False, "in_process": {"ok": False, "difference": ".x: 1 vs 2"},
            "processes": []})
        assert dc.main(["--candles", "60", "--seeds", "0"]) == 1

    def test_a_failing_check_still_prints_its_diagnosis(self, monkeypatch, capsys):
        monkeypatch.setattr(dc, "check", lambda **kwargs: {
            "ok": False, "in_process": {"ok": True, "difference": ""},
            "processes": [{"seed": "0", "matches": False}]})
        dc.main(["--candles", "60", "--seeds", "0"])
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is False
        assert payload["processes"][0]["matches"] is False


class TestDiagnosis:
    """'Different' without a lead is not actionable."""

    def test_in_process_disagreement_points_at_the_engine(self):
        report = {"in_process": {"ok": False, "difference": ".trades[0].pnl: 1 vs 2"},
                  "processes": []}
        text = dc._diagnose(report)
        assert "not hash ordering" in text and ".trades[0].pnl" in text

    def test_cross_process_only_points_at_hash_ordering(self):
        report = {"in_process": {"ok": True, "difference": ""},
                  "processes": [{"seed": "0", "ok": True, "matches": False}]}
        text = dc._diagnose(report)
        assert "PYTHONHASHSEED" in text and "['0']" in text
        assert "set iteration" in text

    def test_a_crashed_child_is_reported_first(self):
        report = {"in_process": {"ok": True, "difference": ""},
                  "processes": [{"seed": "0", "ok": False, "error": "boom",
                                 "matches": False}]}
        assert "could not complete" in dc._diagnose(report)

    def test_a_failing_check_carries_a_diagnosis(self, monkeypatch):
        monkeypatch.setattr(dc, "_subprocess_fingerprint", lambda seed, **kw: {
            "seed": seed, "ok": True, "sha256": "0" * 64, "matches": False})
        report = dc.check(candles=60, seeds=("0",))
        assert report["ok"] is False and "diagnosis" in report

    def test_a_passing_check_has_no_diagnosis(self):
        report = dc.check(candles=60, seeds=())
        assert report["ok"] is True and "diagnosis" not in report
