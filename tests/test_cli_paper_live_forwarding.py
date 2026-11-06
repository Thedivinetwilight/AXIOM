"""Regression: ``python -m app paper-live ...`` must forward its arguments.

``cmd_paper_live`` used to read a module-level ``argv`` that does not exist::

    passthrough = argv[args.passthrough:] if args.passthrough else []

so every invocation *with* arguments died with ``NameError: name 'argv' is not
defined`` before ``run.py`` was ever reached, while an invocation with no
arguments appeared to work. The sub-parser also collected the tail with
``nargs="*"``, which rejects ``--flag value`` pairs it does not declare.

These tests drive the real CLI parser/entry point and replace ``run.main`` with
a recorder, so no engine, feed, broker, database, or order path is touched.
"""

from __future__ import annotations

import sys
import types

import pytest

from app.__main__ import cmd_paper_live, main


@pytest.fixture
def forwarded(monkeypatch):
    """Capture the argv handed to ``run.main`` without importing ``run.py``."""
    calls: list[list[str]] = []

    fake_run = types.ModuleType("run")
    fake_run.main = lambda argv=None: calls.append(list(argv or []))  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "run", fake_run)
    return calls


def test_paper_live_forwards_unknown_flags_and_values(forwarded):
    """``--delay``/``--database`` are run.py's flags, not the app CLI's."""
    with pytest.raises(SystemExit) as exit_info:
        main(["paper-live", "--delay", "0.5", "--database", "sqlite:///paper.db"])

    assert exit_info.value.code == 0
    assert forwarded == [
        ["--mode", "paper", "--source", "upstox", "--delay", "0.5",
         "--database", "sqlite:///paper.db"]
    ]


def test_paper_live_without_arguments_forwards_only_the_paper_pipeline(forwarded):
    with pytest.raises(SystemExit) as exit_info:
        main(["paper-live"])

    assert exit_info.value.code == 0
    assert forwarded == [["--mode", "paper", "--source", "upstox"]]


def test_paper_live_forwards_verbatim_tokens_in_order(forwarded):
    tokens = ["--symbol", "RELIANCE", "--timeframe", "5m", "--strategy", "momentum"]
    with pytest.raises(SystemExit):
        main(["paper-live", *tokens])

    (argv,) = forwarded
    assert argv[0:4] == ["--mode", "paper", "--source", "upstox"]
    assert argv[4:] == tokens, "forwarded tokens must keep their original order"


def test_paper_live_never_requests_a_live_mode(forwarded):
    """The forwarded command is always the PAPER pipeline; live stays off."""
    with pytest.raises(SystemExit):
        main(["paper-live", "--delay", "1.0"])

    (argv,) = forwarded
    assert argv[:4] == ["--mode", "paper", "--source", "upstox"]
    assert "--live" not in argv
    assert not any("mode=live" in token.lower() for token in argv)


def test_cmd_paper_live_tolerates_a_namespace_without_passthrough(forwarded):
    """Direct handlers must not crash on a Namespace built elsewhere."""
    args = types.SimpleNamespace()
    assert cmd_paper_live(args) == 0
    assert forwarded == [["--mode", "paper", "--source", "upstox"]]
