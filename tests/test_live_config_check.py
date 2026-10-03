"""Tests for tools/live_config_check.py.

The point of the tool is to be trustworthy in both directions:

* no false negatives — every reason the application would refuse a live
  configuration must surface as a BLOCKER, and
* no false positives — a configuration the application accepts must not be
  reported as blocked.

The strongest guarantee comes from the design itself (the tool asks
``Settings`` directly), so these tests pin that behaviour, the per-key checks,
and one property that matters more than any of them: a credential must never
appear in the output.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config.settings import Settings, fields as _settings_fields  # noqa: E402
from tools import live_config_check as lcc  # noqa: E402

#: a distinctive value we can search for in the output
FAKE_LIVE_TOKEN = "eyJ-VERY-DISTINCTIVE-LIVE-TOKEN-VALUE-9f3a"
FAKE_MD_TOKEN = "eyJ-VERY-DISTINCTIVE-MD-TOKEN-VALUE-7c1b"

VALID_LIVE = {
    "BROKER": "UPSTOX_LIVE",
    "MODE": "LIVE",
    "LIVE_TRADING": "true",
    "LIVE_CONFIRM": "I-ACCEPT-REAL-MONEY-RISK",
    "UPSTOX_LIVE_ACCESS_TOKEN": FAKE_LIVE_TOKEN,
    "MAX_POSITION_VALUE": "10000",
    "MAX_OPEN_POSITIONS": "1",
    "MAX_DAILY_LOSS": "2000",
    "MAX_TRADES_PER_DAY": "5",
    "LIVE_CANARY_MAX_NOTIONAL": "5000",
    "LIVE_CANARY_ORDERS": "10",
    "LIVE_MAX_EQUITY_DRAWDOWN": "0.05",
    "LIVE_AUTO_STOP_ON_BREACH": "true",
    "LIVE_TOKEN_MAX_AGE_HOURS": "20",
    "DATA_SOURCE": "upstox",
    "MARKET_DATA_PROVIDER": "upstox",
    "MARKET_DATA_ACCESS_TOKEN": FAKE_MD_TOKEN,
    "MARKET_DATA_SYMBOLS": "NSE_EQ|INE002A01018",
    "MARKET_DATA_MODE": "ltpc",
    "MARKET_DATA_TIMEFRAME": "1m",
    "DEFAULT_TIMEFRAME": "1m",
    "MARKET_CALENDAR": "nse-session",
    "DATABASE_URL": "sqlite:///trading_live.db",
    "API_TOKEN": "a-long-private-value",
    "ALERT_WEBHOOK_URL": "https://example.invalid/hook",
}


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch):
    """Exported variables beat the file, so clear every key the app reads.

    Without this, a developer with API_TOKEN or DATABASE_URL exported would
    silently override the file under test.
    """
    for key in lcc.known_env_keys():
        monkeypatch.delenv(key, raising=False)


def write_env(tmp_path, values, name="candidate.env"):
    path = tmp_path / name
    path.write_text("\n".join(f"{k}={v}" for k, v in values.items()) + "\n")
    return path


def keys_of(findings):
    return {item["key"] for item in findings}


class TestValidConfiguration:
    def test_clean_live_config_is_ready(self, tmp_path):
        report = lcc.check(write_env(tmp_path, VALID_LIVE))
        assert report["target"] == "live"
        assert report["blockers"] == []
        assert report["ok"] is True
        assert report["app_verdict"] == "accepted"

    def test_clean_live_config_exits_zero(self, tmp_path, capsys):
        code = lcc.main(["--env-file", str(write_env(tmp_path, VALID_LIVE))])
        assert code == 0
        assert "configuration is acceptable" in capsys.readouterr().out

    def test_agrees_with_settings_construction(self, tmp_path):
        """The tool's 'accepted' must mean Settings really accepts it."""
        path = write_env(tmp_path, VALID_LIVE)
        file_values, effective = lcc.effective_values(path)
        values, errors = lcc.coerce_settings_values(effective)
        assert errors == []
        Settings(**values)  # raises if the tool is wrong about "accepted"

    def test_unverifiable_items_are_notes_not_blockers(self, tmp_path):
        report = lcc.check(write_env(tmp_path, VALID_LIVE))
        assert "STATIC_IP" in keys_of(report["notes"])
        assert report["blockers"] == []


class TestTheFourSwitches:
    @pytest.mark.parametrize("key,value,expected", [
        ("MODE", "PAPER", "MODE"),
        ("LIVE_TRADING", "false", "LIVE_TRADING"),
        ("LIVE_CONFIRM", "yes", "LIVE_CONFIRM"),
    ])
    def test_each_switch_is_a_blocker(self, tmp_path, key, value, expected):
        values = dict(VALID_LIVE, **{key: value})
        report = lcc.check(write_env(tmp_path, values))
        assert report["ok"] is False
        assert expected in keys_of(report["blockers"])

    def test_missing_token_is_a_blocker(self, tmp_path):
        values = dict(VALID_LIVE, UPSTOX_LIVE_ACCESS_TOKEN="")
        report = lcc.check(write_env(tmp_path, values))
        assert "UPSTOX_LIVE_ACCESS_TOKEN" in keys_of(report["blockers"])

    def test_unparseable_boolean_is_a_blocker_not_a_duplicate(self, tmp_path):
        """LIVE_TRADING=1 is the classic trap: the app refuses to parse it."""
        values = dict(VALID_LIVE, LIVE_TRADING="1")
        report = lcc.check(write_env(tmp_path, values))
        assert report["ok"] is False
        assert report["app_verdict"] == "refused"
        mentions = [f for f in report["blockers"] if f["key"] == "LIVE_TRADING"]
        assert len(mentions) == 1, "the parse error is the whole story"
        assert "true or false" in mentions[0]["message"]


class TestRiskLimits:
    @pytest.mark.parametrize("key", ["MAX_POSITION_VALUE", "MAX_OPEN_POSITIONS",
                                     "MAX_DAILY_LOSS", "MAX_TRADES_PER_DAY"])
    def test_zero_limit_is_a_blocker(self, tmp_path, key):
        report = lcc.check(write_env(tmp_path, dict(VALID_LIVE, **{key: "0"})))
        assert report["ok"] is False
        assert key in keys_of(report["blockers"])


class TestHostClassification:
    def test_sandbox_host_for_live_is_a_blocker(self, tmp_path):
        values = dict(VALID_LIVE,
                      UPSTOX_LIVE_BASE_URL="https://api-sandbox.upstox.com")
        report = lcc.check(write_env(tmp_path, values))
        assert "UPSTOX_LIVE_BASE_URL" in keys_of(report["blockers"])
        message = next(f["message"] for f in report["blockers"]
                       if f["key"] == "UPSTOX_LIVE_BASE_URL")
        assert "sandbox" in message

    def test_unknown_host_is_a_blocker(self, tmp_path):
        values = dict(VALID_LIVE, UPSTOX_LIVE_BASE_URL="https://evil.example.com")
        report = lcc.check(write_env(tmp_path, values))
        assert "UPSTOX_LIVE_BASE_URL" in keys_of(report["blockers"])

    def test_honest_live_host_passes(self, tmp_path):
        values = dict(VALID_LIVE,
                      UPSTOX_LIVE_BASE_URL="https://api-hft.upstox.com")
        report = lcc.check(write_env(tmp_path, values))
        assert "UPSTOX_LIVE_BASE_URL" not in keys_of(report["blockers"])


class TestMarketData:
    def test_data_source_must_be_upstox(self, tmp_path):
        report = lcc.check(write_env(tmp_path, dict(VALID_LIVE, DATA_SOURCE="replay")))
        assert "DATA_SOURCE" in keys_of(report["blockers"])

    def test_missing_symbols_is_a_blocker(self, tmp_path):
        report = lcc.check(write_env(tmp_path, dict(VALID_LIVE,
                                                    MARKET_DATA_SYMBOLS="")))
        assert "MARKET_DATA_SYMBOLS" in keys_of(report["blockers"])

    def test_multiple_symbols_warns(self, tmp_path):
        values = dict(VALID_LIVE,
                      MARKET_DATA_SYMBOLS="NSE_EQ|INE002A01018,NSE_EQ|INE009A01021")
        report = lcc.check(write_env(tmp_path, values))
        assert "MARKET_DATA_SYMBOLS" in keys_of(report["warnings"])

    def test_mode_timeframe_mismatch_uses_the_shared_helper(self, tmp_path):
        """full carries 1m/30m bars only — the engine refuses anything else."""
        values = dict(VALID_LIVE, MARKET_DATA_MODE="full",
                      DEFAULT_TIMEFRAME="5m", MARKET_DATA_TIMEFRAME="5m")
        report = lcc.check(write_env(tmp_path, values))
        assert "MARKET_DATA_MODE" in keys_of(report["blockers"])
        message = next(f["message"] for f in report["blockers"]
                       if f["key"] == "MARKET_DATA_MODE")
        assert "1m/30m" in message

    def test_live_timeframe_must_be_intraday(self, tmp_path):
        values = dict(VALID_LIVE, DEFAULT_TIMEFRAME="1D",
                      MARKET_DATA_TIMEFRAME="1D")
        report = lcc.check(write_env(tmp_path, values))
        assert "DEFAULT_TIMEFRAME" in keys_of(report["blockers"])


class TestSilentMistakes:
    def test_mistyped_key_warns_because_it_is_silently_ignored(self, tmp_path):
        values = dict(VALID_LIVE, MAX_POSITION_SIZE="5")
        report = lcc.check(write_env(tmp_path, values))
        assert "MAX_POSITION_SIZE" in keys_of(report["warnings"])

    def test_real_settings_fields_are_recognised(self):
        known = lcc.known_env_keys()
        for name in ("BROKER", "LIVE_TRADING", "MAX_POSITION_VALUE",
                     "LIVE_CANARY_MAX_NOTIONAL", "UPSTOX_LIVE_ACCESS_TOKEN"):
            assert name in known
        assert "NOT_A_REAL_SETTING" not in known

    def test_known_keys_cover_every_settings_field(self):
        """Drift guard: a new field must not be reported as a typo."""
        known = lcc.known_env_keys()
        assert {f.name.upper() for f in _settings_fields(Settings)} <= known

    def test_zero_canary_and_drawdown_warn(self, tmp_path):
        values = dict(VALID_LIVE, LIVE_CANARY_MAX_NOTIONAL="0",
                      LIVE_MAX_EQUITY_DRAWDOWN="0")
        report = lcc.check(write_env(tmp_path, values))
        warned = keys_of(report["warnings"])
        assert {"LIVE_CANARY_MAX_NOTIONAL", "LIVE_MAX_EQUITY_DRAWDOWN"} <= warned
        assert report["blockers"] == []

    def test_api_token_and_paper_database_warn(self, tmp_path):
        values = dict(VALID_LIVE, API_TOKEN="", DATABASE_URL="sqlite:///trading_bot.db")
        report = lcc.check(write_env(tmp_path, values))
        warned = keys_of(report["warnings"])
        assert {"API_TOKEN", "DATABASE_URL"} <= warned


class TestSandboxTarget:
    SANDBOX = {
        "BROKER": "UPSTOX_SANDBOX",
        "MODE": "PAPER",
        "LIVE_TRADING": "false",
        "UPSTOX_SANDBOX_ACCESS_TOKEN": FAKE_MD_TOKEN,
        "DATA_SOURCE": "upstox",
        "MARKET_DATA_PROVIDER": "upstox",
        "MARKET_DATA_ACCESS_TOKEN": FAKE_MD_TOKEN,
        "MARKET_DATA_SYMBOLS": "NSE_EQ|INE002A01018",
        "MARKET_DATA_MODE": "ltpc",
        "DEFAULT_TIMEFRAME": "1m",
        "DATABASE_URL": "sqlite:///trading_sandbox.db",
    }

    def test_sandbox_config_is_ready_and_points_at_sandbox_test(self, tmp_path):
        report = lcc.check(write_env(tmp_path, self.SANDBOX))
        assert report["target"] == "sandbox"
        assert report["blockers"] == []
        assert "sandbox-test" in report["next_command"]

    def test_sandbox_with_live_switches_is_a_blocker(self, tmp_path):
        """Settings permits only MODE=PAPER on the sandbox broker."""
        values = dict(self.SANDBOX, MODE="LIVE", LIVE_TRADING="true")
        report = lcc.check(write_env(tmp_path, values))
        assert report["ok"] is False

    def test_live_order_host_in_sandbox_is_a_blocker(self, tmp_path):
        values = dict(self.SANDBOX,
                      UPSTOX_SANDBOX_BASE_URL="https://api-hft.upstox.com")
        report = lcc.check(write_env(tmp_path, values))
        assert "UPSTOX_SANDBOX_BASE_URL" in keys_of(report["blockers"])

    def test_missing_sandbox_token_is_a_blocker(self, tmp_path):
        values = dict(self.SANDBOX, UPSTOX_SANDBOX_ACCESS_TOKEN="")
        report = lcc.check(write_env(tmp_path, values))
        assert "UPSTOX_SANDBOX_ACCESS_TOKEN" in keys_of(report["blockers"])


class TestNothingToCheck:
    def test_paper_config_reports_not_configured(self, tmp_path, capsys):
        path = write_env(tmp_path, {"BROKER": "LOCAL_PAPER"})
        assert lcc.main(["--env-file", str(path)]) == 2
        assert "nothing to check" in capsys.readouterr().out

    def test_absent_env_file_is_a_note_not_a_crash(self, tmp_path):
        report = lcc.check(tmp_path / "does-not-exist.env")
        assert report["env_file_exists"] is False
        assert report["not_configured"] is True
        assert report["blockers"] == []


class TestApplicationIsTheAuthority:
    """The tool must never report 'ready' for a config the app refuses."""

    def test_app_refusal_becomes_a_blocker_even_when_unnamed(self, tmp_path):
        # an invalid instrument key is validated by Settings, not by the
        # per-key checks — the app's verdict must still block
        values = dict(VALID_LIVE, MARKET_DATA_SYMBOLS="NOT_A_VALID_KEY")
        report = lcc.check(write_env(tmp_path, values))
        assert report["app_verdict"] == "refused"
        assert report["ok"] is False
        assert report["blockers"], "the app's refusal must surface somewhere"

    def test_unnamed_refusal_does_not_duplicate_named_blockers(self, tmp_path):
        values = dict(VALID_LIVE, MAX_DAILY_LOSS="0")
        report = lcc.check(write_env(tmp_path, values))
        assert any(f["key"] == "MAX_DAILY_LOSS" for f in report["blockers"])
        assert not any(f["key"] == "Settings" for f in report["blockers"])


class TestSecretHygiene:
    def _all_text(self, report):
        return lcc.render(report) + json.dumps(report)

    def test_tokens_never_appear_in_the_report(self, tmp_path):
        report = lcc.check(write_env(tmp_path, VALID_LIVE))
        text = self._all_text(report)
        assert FAKE_LIVE_TOKEN not in text
        assert FAKE_MD_TOKEN not in text

    def test_tokens_never_appear_even_when_flagged(self, tmp_path):
        values = dict(VALID_LIVE, UPSTOX_LIVE_ACCESS_TOKEN="CHANGE_ME-NOW-12345")
        report = lcc.check(write_env(tmp_path, values))
        assert "CHANGE_ME-NOW-12345" not in self._all_text(report)
        assert "UPSTOX_LIVE_ACCESS_TOKEN" in keys_of(report["warnings"])

    def test_placeholder_token_warns(self, tmp_path):
        for placeholder in ("CHANGE_ME", "<your-token>", "REHEARSAL-FAKE-x"):
            values = dict(VALID_LIVE, UPSTOX_LIVE_ACCESS_TOKEN=placeholder)
            report = lcc.check(write_env(tmp_path, values))
            assert "UPSTOX_LIVE_ACCESS_TOKEN" in keys_of(report["warnings"]), placeholder

    def test_quoted_whitespace_in_token_warns(self, tmp_path):
        """Surrounding whitespace is sent verbatim, so it must be flagged.

        dotenv strips whitespace around UNQUOTED values, so the file case that
        actually reaches the app is a quoted one.
        """
        values = dict(VALID_LIVE, UPSTOX_LIVE_ACCESS_TOKEN=f'"{FAKE_LIVE_TOKEN} "')
        report = lcc.check(write_env(tmp_path, values))
        messages = " ".join(f["message"] for f in report["warnings"]
                            if f["key"] == "UPSTOX_LIVE_ACCESS_TOKEN")
        assert "whitespace" in messages

    def test_exported_whitespace_in_token_warns(self, tmp_path, monkeypatch):
        """The common real-world case: a token exported with a trailing space
        or the newline left on by `export TOKEN=$(cat token.txt)`."""
        monkeypatch.setenv("UPSTOX_LIVE_ACCESS_TOKEN", f"{FAKE_LIVE_TOKEN}\n")
        report = lcc.check(write_env(tmp_path, VALID_LIVE))
        messages = " ".join(f["message"] for f in report["warnings"]
                            if f["key"] == "UPSTOX_LIVE_ACCESS_TOKEN")
        assert "whitespace" in messages

    def test_unquoted_whitespace_is_stripped_by_the_parser(self, tmp_path):
        """Documents why the check targets quoted/exported values."""
        path = tmp_path / "ws.env"
        path.write_text(f"UPSTOX_LIVE_ACCESS_TOKEN= {FAKE_LIVE_TOKEN} \n")
        assert lcc.read_env_file(path)["UPSTOX_LIVE_ACCESS_TOKEN"] == FAKE_LIVE_TOKEN


class TestEnvironmentPrecedence:
    def test_exported_variable_beats_the_file(self, tmp_path, monkeypatch):
        """Matches load_dotenv(override=False) — the report must not lie."""
        monkeypatch.setenv("MODE", "LIVE")
        report = lcc.check(write_env(tmp_path, VALID_LIVE))
        assert report["app_verdict"] == "accepted"

        # with the export removed the file alone still stands
        monkeypatch.delenv("MODE")
        assert lcc.check(write_env(tmp_path, VALID_LIVE))["ok"] is True

    def test_exported_variable_can_break_the_file_configuration(
            self, tmp_path, monkeypatch):
        monkeypatch.setenv("MODE", "PAPER")
        report = lcc.check(write_env(tmp_path, VALID_LIVE))
        assert report["ok"] is False
        assert "MODE" in keys_of(report["blockers"])


class TestMinimalParser:
    def test_stdlib_parser_handles_comments_quotes_and_blanks(self, tmp_path):
        path = tmp_path / "minimal.env"
        path.write_text(
            "# a comment\n"
            "\n"
            "BROKER=UPSTOX_LIVE\n"
            'MARKET_DATA_SYMBOLS="NSE_EQ|INE002A01018"\n'
            "MAX_DAILY_LOSS=2000\n"
            "no_equals_sign\n"
        )
        values = lcc._minimal_env_parse(path)
        assert values["BROKER"] == "UPSTOX_LIVE"
        assert values["MARKET_DATA_SYMBOLS"] == "NSE_EQ|INE002A01018"
        assert values["MAX_DAILY_LOSS"] == "2000"
        assert "no_equals_sign" not in values

    def test_parser_agrees_with_dotenv_when_available(self, tmp_path):
        pytest.importorskip("dotenv")
        path = tmp_path / "agree.env"
        path.write_text('A="quoted"\nB=plain\nC=\n')
        assert lcc.read_env_file(path) == {"A": "quoted", "B": "plain", "C": ""}
