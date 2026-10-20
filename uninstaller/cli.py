"""Command-line surface of the Arena Deep Uninstaller.

Two modes:

* ``--mode standard`` — remove one installed Arena: its registration, the
  known install/home locations, installer shortcuts and the disposable parts
  of those trees. Preserved categories stay untouched.
* ``--mode deep`` — additionally scan every accessible local fixed drive for
  duplicates, setup packages, extracted application folders, caches and
  leftovers. Portable copies that were never installed are found here.

Nothing destructive happens without a reviewed inventory AND the exact
confirmation phrase (``--confirm-delete DELETE``, or typing it at the prompt).

Exit codes:
  0  completed (or scanned) successfully
  2  usage error
  3  cancelled, or confirmation not given
  4  completed, but some actions failed (all failures are reported)
  5  nothing could be scanned
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Sequence, TextIO

from uninstaller import __version__
from uninstaller.constants import (
    CATEGORY_LABELS,
    CONFIRMATION_PHRASE,
    PROTECTED_CATEGORIES,
    PROTECTED_CATEGORY_FLAGS,
)
from uninstaller.engine import (
    RunOptions,
    ScanRequest,
    apply_inventory,
    build_inventory,
    default_tool_home,
)
from uninstaller.model import Inventory, human_size
from uninstaller.protect import ProtectionPolicy
from uninstaller.quarantine import restore as restore_quarantine
from uninstaller.review import (
    confirm_deletion,
    format_inventory,
    interactive_review,
    selection_summary,
)
from uninstaller.winapi import KnownPaths, Platform, WinRegistryProvider

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_CANCELLED = 3
EXIT_FAILURES = 4
EXIT_NO_SCAN = 5


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="Arena-Uninstall",
        description="Remove an installed AXIOM Arena and find leftover or "
                    "duplicate Arena/AXIOM files across this PC.",
        epilog="A reviewed inventory is always shown before anything is "
               "removed, and deletion requires typing DELETE.",
    )
    parser.add_argument("--version", action="version",
                        version=f"Arena-Uninstall {__version__}")
    parser.add_argument("--mode", choices=("standard", "deep"),
                        help="standard: one installed Arena; deep: scan every "
                             "local fixed drive")
    parser.add_argument("--scan-path", action="append", default=[],
                        metavar="DIR",
                        help="scan this directory in Deep Clean mode "
                             "(repeatable; replaces the drive list)")
    parser.add_argument("--no-drives", action="store_true",
                        help="do not enumerate drives (use with --scan-path)")
    parser.add_argument("--scan-only", action="store_true",
                        help="show the full inventory and change nothing")
    parser.add_argument("--plan", metavar="FILE",
                        help="write the reviewed inventory/plan to FILE and stop")
    parser.add_argument("--apply", metavar="FILE",
                        help="apply a previously reviewed plan file")
    parser.add_argument("--json", metavar="FILE",
                        help="also write the inventory as JSON")
    parser.add_argument("--select-all", action="store_true",
                        help="non-interactive: select every eligible item")
    parser.add_argument("--select-category", action="append", default=[],
                        metavar="CATEGORY")
    parser.add_argument("--deselect-category", action="append", default=[],
                        metavar="CATEGORY")
    for flag, category in PROTECTED_CATEGORY_FLAGS.items():
        parser.add_argument(f"--{flag}", action="store_true",
                            help=f"allow deletion of the preserved category "
                                 f"{category!r} (explicit, off by default)")
    parser.add_argument("--quarantine-dir", metavar="DIR",
                        help="where reversible quarantine items are moved")
    parser.add_argument("--permanent", action="store_true",
                        help="delete instead of quarantining (reported loudly)")
    parser.add_argument("--dry-run", action="store_true",
                        help="show exactly what would happen, change nothing")
    parser.add_argument("--confirm-delete", metavar="PHRASE",
                        help=f"required for non-interactive deletion; must be "
                             f"{CONFIRMATION_PHRASE!r}")
    parser.add_argument("--log-file", metavar="FILE")
    parser.add_argument("--report", metavar="FILE",
                        help="write a JSON action report here")
    parser.add_argument("--home", metavar="DIR",
                        help="this tool's own directory for logs and quarantine "
                             "(default: %LOCALAPPDATA%\\Arena-Uninstall)")
    parser.add_argument("--restore", metavar="DIR",
                        help="restore a quarantine directory created by this tool")
    parser.add_argument("--no-elevation", action="store_true",
                        help="never offer to relaunch elevated")
    parser.add_argument("--elevate", action="store_true",
                        help="non-interactive: relaunch elevated when the scan "
                             "found inaccessible locations")
    parser.add_argument("--interactive", dest="interactive",
                        action="store_true", default=None,
                        help="force the review screen even when stdin/stdout "
                             "are not a terminal")
    parser.add_argument("--no-interactive", dest="interactive",
                        action="store_false",
                        help="never prompt; requires --confirm-delete")
    parser.add_argument("--no-color", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--list-categories", action="store_true",
                        help="print every category and exit")
    return parser


def main(argv: Sequence[str] | None = None, *,
         stdin: TextIO | None = None, stdout: TextIO | None = None,
         platform: Platform | None = None,
         known: KnownPaths | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list_categories:
        for category, label in CATEGORY_LABELS.items():
            marker = " (preserved by default)" if category in PROTECTED_CATEGORIES else ""
            stdout.write(f"{category:<24} {label}{marker}\n")
        return EXIT_OK

    platform = platform or default_platform()

    if args.restore:
        rows = restore_quarantine(Path(args.restore))
        for original, status, detail in rows:
            stdout.write(f"[{status}] {original} {detail}\n".rstrip() + "\n")
        return EXIT_OK if all(status != "failed" for _, status, _ in rows) else EXIT_FAILURES

    known = known or KnownPaths.from_env(platform.environ)
    tool_home = Path(args.home) if args.home else default_tool_home(platform)

    policy = ProtectionPolicy.from_flags(vars(args))
    mode = args.mode
    interactive = (args.interactive if args.interactive is not None
                   else _is_interactive(stdin, stdout))

    if args.apply:
        return _apply_plan(args, policy, platform, tool_home, stdout)

    if mode is None:
        if interactive and not args.scan_only and not args.plan:
            mode = _prompt_mode(stdin, stdout)
        else:
            mode = "standard"

    request = ScanRequest(
        mode=mode,
        extra_roots=[Path(p) for p in args.scan_path],
        include_drives=not args.no_drives,
        progress=(lambda root: stdout.write(f"scanning {root} ...\n")),
    )
    stdout.write(f"Arena Deep Uninstaller {__version__} — mode: {mode}\n")
    inventory = build_inventory(platform, known, request, tool_version=__version__)

    if (mode == "deep" and not inventory.scanned_roots
            and not inventory.matches):
        stdout.write("Nothing could be scanned: no accessible fixed drives and "
                     "no candidate locations were found.\n")
        return EXIT_NO_SCAN

    _print_inventory(inventory, policy, stdout)

    if args.json:
        _write_json(Path(args.json), inventory.to_json_text(), stdout)
    if args.plan:
        _write_json(Path(args.plan), inventory.to_json_text(), stdout)
        stdout.write("\nPlan written. Review it, then apply it with:\n"
                     f"  Arena-Uninstall --apply {args.plan} "
                     f"--confirm-delete {CONFIRMATION_PHRASE}\n")
        return EXIT_OK
    if args.scan_only:
        return EXIT_OK

    _apply_selection_flags(args, inventory, stdout)

    if inventory.needs_elevation and not args.no_elevation and not platform.is_admin():
        if interactive:
            stdout.write("\nSome locations were not readable without "
                         "administrator rights.\nRelaunch elevated to include "
                         "them? [y/N] ")
            stdout.flush()
            answer = stdin.readline().strip().lower()
            if answer in ("y", "yes"):
                ok, detail = platform.relaunch_elevated(argv)
                stdout.write(f"{detail}\n")
                return EXIT_OK if ok else EXIT_FAILURES
        elif args.elevate:
            ok, detail = platform.relaunch_elevated(
                [a for a in argv if a != "--elevate"] + ["--elevate"])
            stdout.write(f"{detail}\n")
            return EXIT_OK if ok else EXIT_FAILURES
        stdout.write("Continuing without elevation; inaccessible locations are "
                     "reported in the log.\n")

    if interactive:
        outcome = interactive_review(inventory, policy, stdin, stdout)
        if outcome != "proceed":
            return EXIT_CANCELLED
        stdout.write("\n" + selection_summary(inventory, policy) + "\n")
        if not confirm_deletion(stdin, stdout):
            return EXIT_CANCELLED
    else:
        policy.enforce_selection(inventory)
        if not inventory.selected():
            stdout.write("Nothing is selected; nothing to do.\n")
            return EXIT_CANCELLED
        if args.confirm_delete != CONFIRMATION_PHRASE:
            stdout.write(
                "Refusing to delete without confirmation. Re-run with "
                f"--confirm-delete {CONFIRMATION_PHRASE} after reviewing the "
                "inventory above.\n")
            return EXIT_CANCELLED

    return _execute(inventory, policy, platform, tool_home, args, stdout)


def default_platform() -> Platform:
    registry = WinRegistryProvider() if os.name == "nt" else None
    return Platform(registry=registry) if registry else Platform()


def _is_interactive(stdin: TextIO, stdout: TextIO) -> bool:
    try:
        return bool(stdin.isatty() and stdout.isatty())
    except (AttributeError, ValueError):
        return False


def _prompt_mode(stdin: TextIO, stdout: TextIO) -> str:
    stdout.write(
        "\nWhat would you like to do?\n"
        "  1) Standard uninstall — remove the installed Arena (keeps your data)\n"
        "  2) Deep clean — also scan all local drives for leftovers and duplicates\n"
        "Choose [1/2] (default 1): ")
    stdout.flush()
    answer = stdin.readline().strip()
    return "deep" if answer == "2" else "standard"


def _print_inventory(inventory: Inventory, policy: ProtectionPolicy,
                     stdout: TextIO) -> None:
    stdout.write("\n" + format_inventory(inventory, policy) + "\n")
    if inventory.issues:
        denied = [i for i in inventory.issues if i.kind == "access-denied"]
        stdout.write(f"\n{len(inventory.issues)} scan issue(s) recorded"
                     + (f", {len(denied)} access-denied location(s)" if denied else "")
                     + " — see the log.\n")
    stdout.write("\n" + selection_summary(inventory, policy) + "\n")


def _apply_selection_flags(args, inventory: Inventory, stdout: TextIO) -> None:
    if args.select_all:
        for match in inventory.matches:
            if match.selectable and not match.ambiguous:
                match.selected = True
    for category in args.select_category:
        count = 0
        for match in inventory.matches:
            if match.category == category and match.selectable:
                match.selected = True
                count += 1
        stdout.write(f"selected {count} item(s) in category {category}\n")
    for category in args.deselect_category:
        count = 0
        for match in inventory.matches:
            if match.category == category:
                match.selected = False
                count += 1
        stdout.write(f"deselected {count} item(s) in category {category}\n")


def _execute(inventory: Inventory, policy: ProtectionPolicy, platform: Platform,
             tool_home: Path, args, stdout: TextIO) -> int:
    options = RunOptions(
        permanent=args.permanent,
        dry_run=args.dry_run,
        quarantine_root=Path(args.quarantine_dir) if args.quarantine_dir else None,
        log_path=Path(args.log_file) if args.log_file else None,
    )
    stdout.write("\n" + ("DRY RUN — no changes will be made.\n" if args.dry_run else "")
                 + selection_summary(inventory, policy) + "\n")
    report, _log = apply_inventory(inventory, policy, platform, tool_home, options)

    if args.report:
        try:
            Path(args.report).write_text(report.to_json_text(), encoding="utf-8")
        except OSError as exc:
            stdout.write(f"could not write the report: {exc}\n")

    grouped: dict[str, list] = {}
    for result in report.results:
        grouped.setdefault(result.action, []).append(result)
    for action in ("quarantined", "deleted", "deferred", "registry-removed",
                   "skipped", "failed"):
        items = grouped.get(action, [])
        if not items:
            continue
        total = sum(1 for _ in items)
        stdout.write(f"\n{action}: {total} item(s)\n")
        for result in items[:200]:
            stdout.write(f"  [{result.category}] {result.path}\n")
            if result.detail:
                stdout.write(f"      {result.detail}\n")
        if total > 200:
            stdout.write(f"  ... {total - 200} more (see the log)\n")

    if report.quarantine_dir:
        stdout.write(f"\nQuarantine: {report.quarantine_dir}\n"
                     "Everything quarantined can be restored with:\n"
                     f"  Arena-Uninstall --restore \"{report.quarantine_dir}\"\n")
    if report.permanent_deletions:
        stdout.write(f"\nPERMANENTLY DELETED: {len(report.permanent_deletions)} "
                     "item(s) — these cannot be restored.\n")
    if report.log_path:
        stdout.write(f"\nCleanup log: {report.log_path}\n")
    if report.failures:
        stdout.write(f"\n{len(report.failures)} item(s) could not be removed "
                     "(reported above; nothing was retried destructively).\n")
        return EXIT_FAILURES
    return EXIT_OK


def _apply_plan(args, policy: ProtectionPolicy, platform: Platform,
                tool_home: Path, stdout: TextIO) -> int:
    try:
        inventory = Inventory.from_plan(
            json.loads(Path(args.apply).read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        stdout.write(f"cannot read the plan: {exc}\n")
        return EXIT_USAGE
    policy.enforce_selection(inventory)
    _print_inventory(inventory, policy, stdout)
    if args.select_all:
        _apply_selection_flags(args, inventory, stdout)
    if args.confirm_delete != CONFIRMATION_PHRASE:
        stdout.write(
            "Refusing to delete without confirmation. Re-run with "
            f"--confirm-delete {CONFIRMATION_PHRASE}.\n")
        return EXIT_CANCELLED
    if not inventory.selected():
        stdout.write("Nothing is selected; nothing to do.\n")
        return EXIT_CANCELLED
    return _execute(inventory, policy, platform, tool_home, args, stdout)


def _write_json(path: Path, text: str, stdout: TextIO) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        stdout.write(f"\ninventory written to {path} "
                     f"({human_size(len(text.encode('utf-8')))})\n")
    except OSError as exc:
        stdout.write(f"could not write {path}: {exc}\n")


def run() -> int:
    try:
        return main()
    except KeyboardInterrupt:
        sys.stdout.write("\nCancelled — no further changes were made.\n")
        return EXIT_CANCELLED


if __name__ == "__main__":
    raise SystemExit(run())
