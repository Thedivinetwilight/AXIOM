"""SQLite URL/path parsing — the startup path must never guess silently."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.database.paths import (
    DatabaseUrlError,
    absolute_database_url,
    is_absolute_path_text,
    is_memory_url,
    resolve_sqlite_path,
    sqlite_path_text,
)


class TestParsing:
    def test_relative_is_anchored_to_the_home(self):
        path = resolve_sqlite_path("sqlite:///trading_bot.db", home="/home/me/Arena")
        assert path == Path("/home/me/Arena/trading_bot.db")

    def test_relative_uses_the_cwd_without_a_home(self, tmp_path):
        assert resolve_sqlite_path("sqlite:///x.db", base=tmp_path) == \
            (tmp_path / "x.db").resolve()

    def test_absolute_posix_path_is_preserved(self):
        assert resolve_sqlite_path("sqlite:////var/lib/arena/a.db") == \
            Path("/var/lib/arena/a.db")

    def test_windows_drive_path_is_absolute_even_on_posix(self):
        assert is_absolute_path_text("C:/Users/me/arena.db")
        assert is_absolute_path_text(r"C:\\Users\\me\\arena.db")
        assert sqlite_path_text("sqlite:///C:/Users/me/arena.db") == \
            "C:/Users/me/arena.db"

    def test_unc_path_is_recognised(self):
        assert is_absolute_path_text("//server/share/arena.db")
        assert is_absolute_path_text(r"\\\\server\\share\\arena.db")

    def test_memory_database_has_no_path(self):
        assert is_memory_url("sqlite:///:memory:")
        with pytest.raises(DatabaseUrlError):
            resolve_sqlite_path("sqlite:///:memory:")

    def test_query_and_fragment_are_refused(self):
        for url in ("sqlite:///a.db?mode=ro", "sqlite:///a.db#fragment"):
            with pytest.raises(DatabaseUrlError):
                sqlite_path_text(url)

    def test_non_sqlite_and_empty_paths_are_refused(self):
        for url in ("postgresql://host/db", "sqlite:///", ""):
            with pytest.raises(DatabaseUrlError):
                sqlite_path_text(url)

    def test_absolute_url_round_trip(self, tmp_path):
        url = absolute_database_url("sqlite:///relative.db", home=tmp_path)
        assert url == f"sqlite:///{tmp_path / 'relative.db'}"
        assert sqlite_path_text(url) == str(tmp_path / "relative.db")
