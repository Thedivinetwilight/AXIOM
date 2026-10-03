"""Introspection: turn the real command-line parsers into a UI catalogue.

The console does NOT maintain its own list of features. It reads the argument
parsers that `app.__main__` and `run.py` actually use, so the moment a new
subcommand or flag is added to the CLI it appears in the GUI automatically,
and a feature that is deleted from the CLI cannot linger in the GUI as a
button that does nothing.

That property is the whole point: a hand-written catalogue drifts, and a
trading console that silently disagrees with the CLI is worse than no console.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
from typing import Any

# Commands that do not return on their own: they run until something stops
# them. The console still runs them (bounded by a timeout and killable), but
# it labels them so an operator is not surprised by a button that "never
# finishes".
LONG_RUNNING = {
    "market-stream": "streams until --duration elapses",
    "paper-live": "runs the paper trading loop until stopped",
    "engine": "runs the execution engine until stopped",
    # NOTE: `monitor` is deliberately NOT here — despite the name it is a
    # one-shot snapshot that exits (with status 1 when it has alerts).
}

# Commands that reach a broker. Running one is not itself a trade — the app's
# own gates decide what is permitted — but the console marks them so the
# operator knows they leave the machine.
REACHES_NETWORK = {
    "upstox-test",
    "upstox-login",
    "market-stream",
    "paper-live",
    "engine",
    "live-preflight",
    "live-drill",
    "sandbox-test",
    "initialize",
    "history",
    "market-universe",
    "universe",
    "dataset",
    "research",
    "discover",
    "scan",
    "lifecycle",
    "backup",
    "report",
}

# Commands that can move a strategy's lifecycle state or place an order.
STATE_CHANGING = {
    "sandbox-test",
    "live-halt",
    "live-resume",
    "live-ack",
    "lifecycle",
    "engine",
    "paper-live",
}


@dataclass
class Option:
    flag: str
    dest: str
    help: str = ""
    type: str = "str"
    default: Any = None
    choices: list[str] | None = None
    is_flag: bool = False
    required: bool = False
    metavar: str | None = None


@dataclass
class Positional:
    name: str
    help: str = ""
    choices: list[str] | None = None
    required: bool = True
    variadic: bool = False


@dataclass
class Command:
    name: str
    label: str
    summary: str
    positionals: list[Positional] = field(default_factory=list)
    options: list[Option] = field(default_factory=list)
    long_running: str | None = None
    reaches_network: bool = False
    state_changing: bool = False
    program: str = "app"


def _type_name(action: argparse.Action) -> str:
    if action.type is None:
        return "str"
    return getattr(action.type, "__name__", "str")


def _choices(action: argparse.Action) -> list[str] | None:
    if not action.choices:
        return None
    return [str(choice) for choice in action.choices]


def _clean_default(action: argparse.Action) -> Any:
    value = action.default
    if value is None or value is argparse.SUPPRESS:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _describe(parser: argparse.ArgumentParser) -> str:
    text = (parser.description or getattr(parser, "help", "") or "").strip()
    return " ".join(text.split())


def command_from_parser(
    name: str,
    parser: argparse.ArgumentParser,
    *,
    program: str = "app",
) -> Command:
    positionals: list[Positional] = []
    options: list[Option] = []

    for action in parser._actions:
        if action.dest in {"help", "command", "func"}:
            continue
        if action.dest == argparse.SUPPRESS:
            continue
        help_text = " ".join((action.help or "").split())

        if not action.option_strings:
            positionals.append(
                Positional(
                    name=action.dest,
                    help=help_text,
                    choices=_choices(action),
                    required=action.nargs not in ("*", "?"),
                    variadic=action.nargs in ("*", "+", argparse.REMAINDER),
                )
            )
            continue

        flag = action.option_strings[0]
        is_flag = isinstance(action, (argparse._StoreTrueAction, argparse._StoreFalseAction))
        options.append(
            Option(
                flag=flag,
                dest=action.dest,
                help=help_text,
                type=_type_name(action),
                default=_clean_default(action),
                choices=_choices(action),
                is_flag=is_flag,
                required=bool(getattr(action, "required", False)),
                metavar=getattr(action, "metavar", None),
            )
        )

    return Command(
        name=name,
        label=name,
        summary=_describe(parser),
        positionals=positionals,
        options=options,
        long_running=LONG_RUNNING.get(name),
        reaches_network=name in REACHES_NETWORK,
        state_changing=name in STATE_CHANGING,
        program=program,
    )


def cli_commands() -> list[Command]:
    """Every `python -m app <command>` subcommand, read from the live parser.

    The one-line summary comes from the SAME `help=` string that
    `python -m app --help` prints, so the console and the terminal can never
    describe a command differently.
    """
    from app.__main__ import parser as app_parser

    parser = app_parser()
    found: list[Command] = []
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        # argparse stores each subcommand's help in a pseudo-action on the
        # parent; the subparser object itself carries no description.
        summaries = {
            pseudo.dest: " ".join((pseudo.help or "").split())
            for pseudo in getattr(action, "_choices_actions", [])
        }
        for name, sub in action.choices.items():
            command = command_from_parser(name, sub, program="app")
            command.summary = summaries.get(name) or command.summary
            found.append(command)
    return found


def run_modes() -> list[Command]:
    """Every `python run.py --mode <mode>` mode, read from the live parser.

    Exposed as one command per mode rather than one command with a `--mode`
    flag, because picking a mode is the primary choice an operator makes and
    each mode deserves its own entry and summary in the UI.
    """
    from run import parser as run_parser

    parser = run_parser()
    mode_action = next(
        (a for a in parser._actions if a.dest == "mode"), None
    )
    modes = [str(c) for c in (mode_action.choices or [])] if mode_action else []

    shared: list[Option] = []
    for action in parser._actions:
        if action.dest in {"help", "mode"} or not action.option_strings:
            continue
        is_flag = isinstance(action, (argparse._StoreTrueAction, argparse._StoreFalseAction))
        shared.append(
            Option(
                flag=action.option_strings[0],
                dest=action.dest,
                help=" ".join((action.help or "").split()),
                type=_type_name(action),
                default=_clean_default(action),
                choices=_choices(action),
                is_flag=is_flag,
                required=bool(getattr(action, "required", False)),
                metavar=getattr(action, "metavar", None),
            )
        )

    summaries = {
        "backtest": "Run one strategy over a CSV or recorded feed and report metrics.",
        "tournament": "Rank every strategy against each other on the same data.",
        "validate": "Check a strategy against held-out data.",
        "walk-forward": "Rolling train/test splits to expose overfitting.",
        "robustness": "Perturb inputs (costs, slippage, noise) to test fragility.",
        "paper": "Trade the simulated account live against a feed. No real money.",
        "api": "Serve the HTTP API and the browser console.",
    }

    commands: list[Command] = []
    for mode in modes:
        commands.append(
            Command(
                name=mode,
                label=mode,
                summary=summaries.get(mode, f"`run.py --mode {mode}`"),
                positionals=[],
                options=[Option(**{**asdict(o), **{"default": o.default}}) for o in shared],
                long_running=(
                    "runs continuously until stopped" if mode in {"paper", "api"} else None
                ),
                reaches_network=mode in {"paper", "api"},
                state_changing=mode == "paper",
                program="run",
            )
        )
    return commands


def catalogue() -> dict:
    """The complete feature catalogue the console renders."""
    cli = cli_commands()
    modes = run_modes()
    return {
        "cli": [asdict(c) for c in sorted(cli, key=lambda c: c.name)],
        "modes": [asdict(c) for c in modes],
        "counts": {"cli": len(cli), "modes": len(modes), "total": len(cli) + len(modes)},
    }
