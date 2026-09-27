"""3.0.2 configuration-parser regression battery.

Root cause being locked in: `Settings.from_env()` used to branch on the
RUNTIME TYPE OF THE DEFAULT VALUE (`isinstance(default, bool/float)`), so
int-annotated fields and float fields with int defaults (e.g.
`initial_capital: float = 100_000`) leaked raw strings into validation and
execution code — `TypeError: must be real number, not str` (dashboard boot)
and `'<' not supported between instances of 'str' and 'int'`.

The parser now converts EVERY field according to its DECLARED annotation
(`dataclasses.fields()` + `typing.get_type_hints`). These tests pin that
contract, the full required value matrix, malformed-value behavior (clean
field-named ValueErrors, never TypeErrors), aliases, and — unchanged — every
safety restriction (PAPER-only, LIVE rejected, sandbox-token requirement).
"""

from __future__ import annotations

import dataclasses
import typing

import pytest

from app.config.settings import Settings, coerce_env_value


@pytest.fixture(autouse=True)
def clean_config_env(monkeypatch):
    """Isolate every test from the operator environment and any repo .env."""
    import os

    saved = {k: v for k, v in os.environ.items()
             if k.upper() in {f.name.upper() for f in dataclasses.fields(Settings)}
             or k in {"UPSTOX_ACCESS_TOKEN", "UPSTOX_INSTRUMENT_KEYS"}}
    for key in list(os.environ):
        upper = key.upper()
        if upper in {f.name.upper() for f in dataclasses.fields(Settings)} \
                or key in {"UPSTOX_ACCESS_TOKEN", "UPSTOX_INSTRUMENT_KEYS"}:
            monkeypatch.delenv(key, raising=False)
    yield
    for key in list(os.environ):
        upper = key.upper()
        if upper in {f.name.upper() for f in dataclasses.fields(Settings)} \
                or key in {"UPSTOX_ACCESS_TOKEN", "UPSTOX_INSTRUMENT_KEYS"}:
            monkeypatch.delenv(key, raising=False)
    for key, value in saved.items():
        os.environ[key] = value


# ------------------------------------------------- required value matrix ----

class TestRequiredMatrix:
    def test_initial_capital_string_env_becomes_float(self, monkeypatch):
        monkeypatch.setenv("INITIAL_CAPITAL", "100000")
        settings = Settings.from_env()
        assert settings.initial_capital == 100000.0
        assert isinstance(settings.initial_capital, float)

    def test_max_open_positions_env_becomes_int(self, monkeypatch):
        monkeypatch.setenv("MAX_OPEN_POSITIONS", "5")
        settings = Settings.from_env()
        assert settings.max_open_positions == 5
        assert isinstance(settings.max_open_positions, int)
        assert not isinstance(settings.max_open_positions, bool)

    def test_max_trades_per_day_env_becomes_int(self, monkeypatch):
        monkeypatch.setenv("MAX_TRADES_PER_DAY", "10")
        settings = Settings.from_env()
        assert settings.max_trades_per_day == 10
        assert isinstance(settings.max_trades_per_day, int)

    def test_live_trading_false_parses_false(self, monkeypatch):
        monkeypatch.setenv("LIVE_TRADING", "false")
        settings = Settings.from_env()
        assert settings.live_trading is False

    def test_live_trading_true_converts_to_true_at_parser(self, monkeypatch):
        """The PARSER converts 'true' → True; the SAFETY validation then
        rejects it (separate test). Conversion and safety are distinct layers."""
        assert coerce_env_value("live_trading",
                                typing.get_type_hints(Settings)["live_trading"],
                                "true") is True

    def test_live_trading_true_rejected_by_settings_validation(self, monkeypatch):
        monkeypatch.setenv("LIVE_TRADING", "true")
        with pytest.raises(ValueError, match="Only MODE=PAPER"):
            Settings.from_env()

    def test_position_size_env_becomes_float(self, monkeypatch):
        monkeypatch.setenv("POSITION_SIZE", "0.25")
        settings = Settings.from_env()
        assert settings.position_size == 0.25
        assert isinstance(settings.position_size, float)

    def test_stop_loss_env_becomes_float(self, monkeypatch):
        monkeypatch.setenv("STOP_LOSS", "0.05")
        assert Settings.from_env().stop_loss == 0.05

    def test_stop_loss_none_becomes_none(self, monkeypatch):
        monkeypatch.setenv("STOP_LOSS", "none")
        assert Settings.from_env().stop_loss is None

    def test_take_profit_none_becomes_none(self, monkeypatch):
        monkeypatch.setenv("TAKE_PROFIT", "none")
        assert Settings.from_env().take_profit is None

    def test_mode_stays_string(self, monkeypatch):
        monkeypatch.setenv("MODE", "PAPER")
        settings = Settings.from_env()
        assert settings.mode == "PAPER"
        assert isinstance(settings.mode, str)

    def test_broker_local_paper_valid(self, monkeypatch):
        monkeypatch.setenv("BROKER", "LOCAL_PAPER")
        assert Settings.from_env().broker == "LOCAL_PAPER"

    def test_broker_sandbox_without_token_rejected(self, monkeypatch):
        monkeypatch.setenv("BROKER", "UPSTOX_SANDBOX")
        monkeypatch.delenv("UPSTOX_SANDBOX_ACCESS_TOKEN", raising=False)
        with pytest.raises(ValueError,
                           match="requires UPSTOX_SANDBOX_ACCESS_TOKEN"):
            Settings.from_env()

    def test_broker_live_rejected(self, monkeypatch):
        monkeypatch.setenv("BROKER", "LIVE")
        with pytest.raises(ValueError, match="LIVE trading is not enabled"):
            Settings.from_env()

    def test_broker_live_lowercase_also_rejected(self, monkeypatch):
        monkeypatch.setenv("BROKER", "live")
        with pytest.raises(ValueError, match="LIVE trading is not enabled"):
            Settings.from_env()


# ---------------------------------------------------- malformed values ------

class TestMalformedValuesAreCleanValueErrors:
    def test_initial_capital_abc(self, monkeypatch):
        monkeypatch.setenv("INITIAL_CAPITAL", "abc")
        with pytest.raises(ValueError, match="INITIAL_CAPITAL must be a number"):
            Settings.from_env()

    def test_max_open_positions_abc(self, monkeypatch):
        monkeypatch.setenv("MAX_OPEN_POSITIONS", "abc")
        with pytest.raises(ValueError, match="MAX_OPEN_POSITIONS must be an integer"):
            Settings.from_env()

    def test_max_trades_per_day_fraction_rejected(self, monkeypatch):
        monkeypatch.setenv("MAX_TRADES_PER_DAY", "3.5")
        with pytest.raises(ValueError, match="MAX_TRADES_PER_DAY must be an integer"):
            Settings.from_env()

    def test_position_size_abc(self, monkeypatch):
        monkeypatch.setenv("POSITION_SIZE", "abc")
        with pytest.raises(ValueError, match="POSITION_SIZE must be a number"):
            Settings.from_env()

    def test_stop_loss_abc_optional_float(self, monkeypatch):
        monkeypatch.setenv("STOP_LOSS", "abc")
        with pytest.raises(ValueError, match="STOP_LOSS must be a number"):
            Settings.from_env()

    def test_live_trading_maybe(self, monkeypatch):
        monkeypatch.setenv("LIVE_TRADING", "maybe")
        with pytest.raises(ValueError, match="LIVE_TRADING must be true or false"):
            Settings.from_env()

    def test_maintenance_hour_abc(self, monkeypatch):
        monkeypatch.setenv("MAINTENANCE_HOUR", "abc")
        with pytest.raises(ValueError, match="MAINTENANCE_HOUR must be an integer"):
            Settings.from_env()


# --------------------------------------- every field typed by declaration ----

class TestEveryFieldMatchesDeclaration:
    def test_from_env_defaults_are_correctly_typed(self):
        hints = typing.get_type_hints(Settings)
        settings = Settings.from_env()
        for f in dataclasses.fields(Settings):
            value = getattr(settings, f.name)
            is_optional, base = _base(hints[f.name])
            if base is bool:
                assert isinstance(value, bool), f.name
            elif base is int:
                assert isinstance(value, int) and not isinstance(value, bool), f.name
            elif base is float:
                assert isinstance(value, float), f"{f.name}: {value!r}"
            elif base is str:
                assert isinstance(value, str), f.name

    def test_plain_constructor_defaults_are_correctly_typed(self):
        hints = typing.get_type_hints(Settings)
        settings = Settings()
        for f in dataclasses.fields(Settings):
            value = getattr(settings, f.name)
            _is_optional, base = _base(hints[f.name])
            if base is bool:
                assert isinstance(value, bool), f.name
            elif base is int:
                assert isinstance(value, int) and not isinstance(value, bool), f.name
            elif base is float:
                assert isinstance(value, float), f"{f.name}: {value!r} must be float"
            elif base is str:
                assert isinstance(value, str), f.name

    def test_every_env_set_field_matches_declaration(self, monkeypatch):
        """Set every field that has a scalar env representation and verify
        the parsed runtime type against the annotation."""
        hints = typing.get_type_hints(Settings)
        env_values = {
            "MODE": "PAPER", "LIVE_TRADING": "false", "INITIAL_CAPITAL": "250000",
            "CURRENCY": "INR", "MARKET_CALENDAR": "continuous",
            "DEFAULT_SYMBOL": "DEMO", "DEFAULT_TIMEFRAME": "1D",
            "COMMISSION": "0.001", "SLIPPAGE": "0.001", "SPREAD": "0.0003",
            "POSITION_SIZE": "0.5", "STOP_LOSS": "0.04", "TAKE_PROFIT": "0.2",
            "MAX_PARTICIPATION": "0.05", "MAX_POSITION_VALUE": "10000",
            "MAX_OPEN_POSITIONS": "2", "MAX_DAILY_LOSS": "1500",
            "MAX_TRADES_PER_DAY": "7", "MAINTENANCE_HOUR": "18",
            "MAX_DOWNLOAD_RETRIES": "4", "RESEARCH_BATCH_SIZE": "25",
            "MAX_STRATEGY_ALLOCATION": "0.3", "EVENT_MOVE_THRESHOLD": "0.004",
            "EVENT_VOLUME_SPIKE": "2.5", "SCANNER_TOP_K": "3",
            "DISCOVERY_MAX_CANDIDATES": "8", "PARQUET_ENABLED": "false",
        }
        for key, value in env_values.items():
            monkeypatch.setenv(key, value)
        settings = Settings.from_env()
        for key in env_values:
            name = key.lower()
            value = getattr(settings, name)
            _is_optional, base = _base(hints[name])
            if base is bool:
                assert isinstance(value, bool), name
            elif base is int:
                assert isinstance(value, int) and not isinstance(value, bool), name
            elif base is float:
                assert isinstance(value, float), name
            else:
                assert isinstance(value, str), name
        assert settings.initial_capital == 250000.0
        assert settings.max_open_positions == 2
        assert settings.max_trades_per_day == 7
        assert settings.maintenance_hour == 18
        assert settings.parquet_enabled is False
        assert settings.stop_loss == 0.04


def _base(annotation):
    from app.config.settings import _unwrap_optional

    return _unwrap_optional(annotation)


# -------------------------------------------------------------- aliases -----

class TestAliases:
    def test_upstox_access_token_alias_resolves(self, monkeypatch):
        monkeypatch.setenv("UPSTOX_ACCESS_TOKEN", "alias-token")
        assert Settings.from_env().market_data_access_token == "alias-token"

    def test_upstox_instrument_keys_alias_resolves(self, monkeypatch):
        monkeypatch.setenv("UPSTOX_INSTRUMENT_KEYS", "NSE_EQ|INE002A01018")
        assert Settings.from_env().market_data_symbols == "NSE_EQ|INE002A01018"

    def test_explicit_market_data_var_wins_over_alias(self, monkeypatch):
        monkeypatch.setenv("UPSTOX_ACCESS_TOKEN", "alias-token")
        monkeypatch.setenv("MARKET_DATA_ACCESS_TOKEN", "explicit-token")
        assert Settings.from_env().market_data_access_token == "explicit-token"

    def test_field_default_style_secrets_remain_strings(self, monkeypatch):
        monkeypatch.setenv("API_TOKEN", "secret-value")
        settings = Settings.from_env()
        assert settings.api_token == "secret-value"
        assert isinstance(settings.api_token, str)


# ------------------------------------------------- coercion unit contract ---

class TestCoerceEnvValueContract:
    hints = typing.get_type_hints(Settings)

    def test_bool_true_false_case_insensitive(self):
        ann = self.hints["live_trading"]
        assert coerce_env_value("live_trading", ann, "TRUE") is True
        assert coerce_env_value("live_trading", ann, "False") is False

    def test_bool_invalid(self):
        ann = self.hints["live_trading"]
        with pytest.raises(ValueError, match="must be true or false"):
            coerce_env_value("live_trading", ann, "1")

    def test_int_negative_and_zero(self):
        ann = self.hints["max_trades_per_day"]
        assert coerce_env_value("max_trades_per_day", ann, "0") == 0
        assert coerce_env_value("max_trades_per_day", ann, "-1") == -1

    def test_optional_float_none_only_for_optional_fields(self):
        ann = self.hints["stop_loss"]
        assert coerce_env_value("stop_loss", ann, "NONE") is None
        assert coerce_env_value("stop_loss", ann, "None") is None

    def test_non_optional_float_rejects_none_word(self):
        ann = self.hints["commission"]
        with pytest.raises(ValueError, match="COMMISSION must be a number"):
            coerce_env_value("commission", ann, "none")

    def test_string_passthrough(self):
        ann = self.hints["api_token"]
        assert coerce_env_value("api_token", ann, "Bearer abc|123") == "Bearer abc|123"

    def test_programmatic_int_default_normalized_for_float_annotation(self):
        ann = self.hints["initial_capital"]
        assert coerce_env_value("initial_capital", ann, 100_000) == 100000.0
        assert isinstance(coerce_env_value("initial_capital", ann, 100_000), float)


# ------------------------------------------------ safety must be unchanged --

class TestSafetyUnchanged:
    def test_mode_not_paper_rejected(self, monkeypatch):
        monkeypatch.setenv("MODE", "LIVE")
        with pytest.raises(ValueError, match="Only MODE=PAPER"):
            Settings.from_env()

    def test_non_sqlite_database_rejected(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://x")
        with pytest.raises(ValueError, match="Only SQLite is supported"):
            Settings.from_env()

    def test_negative_risk_limit_rejected(self, monkeypatch):
        monkeypatch.setenv("MAX_DAILY_LOSS", "-5")
        with pytest.raises(ValueError, match="cannot be negative"):
            Settings.from_env()

    def test_sandbox_product_validation_unchanged(self, monkeypatch):
        monkeypatch.setenv("SANDBOX_PRODUCT", "X")
        with pytest.raises(ValueError, match="SANDBOX_PRODUCT"):
            Settings.from_env()

    def test_bad_instrument_key_rejected_for_upstox_source(self, monkeypatch):
        monkeypatch.setenv("DATA_SOURCE", "upstox")
        monkeypatch.setenv("MARKET_DATA_SYMBOLS", "RELIANCE")
        with pytest.raises(ValueError, match="instrument"):
            Settings.from_env()
