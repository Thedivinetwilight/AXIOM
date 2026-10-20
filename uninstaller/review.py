"""Terminal review surface: the complete inventory, per-item control.

Nothing is deleted while this screen is up. The operator sees every match
with its path, size, category and reason; selection can be toggled per item
or per category; typing ``q`` cancels with no changes at all; the only way
forward is the exact confirmation phrase.

The same formatting is used for non-interactive runs, so a CI log of an
unattended uninstall reads identically to the interactive session.
"""

from __future__ import annotations

import sys
from typing import TextIO

from uninstaller.constants import (
    CONFIRMATION_PHRASE,
    PROTECTED_CATEGORIES,
)
from uninstaller.model import Inventory, human_size
from uninstaller.protect import ProtectionPolicy

MENU_HELP = """\
Commands:
  s N[,M-K...]   select item(s)      d N[,M-K...]   deselect item(s)
  sa             select all eligible  sn             select none
  cat <name> on|off   toggle a whole category
  protect on|off      enable deletion of preserved categories (explicit)
  l                    list again
  p                    proceed to confirmation
  q                    cancel — nothing is changed"""


def format_inventory(inventory: Inventory, policy: ProtectionPolicy,
                     limit: int | None = None) -> str:
    lines = []
    lines.append(f"Mode: {inventory.mode}    matches: {len(inventory.matches)}")
    if inventory.needs_elevation:
        lines.append("NOTE: some locations were inaccessible without elevation.")
    lines.append(f"{'#':>4}  {'Sel':^3}  {'Category':<24} {'Size':>10}  Path")
    lines.append("-" * 100)
    shown = inventory.matches if limit is None else inventory.matches[:limit]
    for i, match in enumerate(shown, start=1):
        sel = "x" if match.selected else " "
        category = match.category
        lines.append(f"{i:>4}  {sel:^3}  {category:<24} "
                     f"{human_size(match.size_bytes):>10}  {match.path}")
        for reason in match.reasons:
            lines.append(f"{'':>4}  {'':^3}  {'':<24} {'':>10}    reason: {reason}")
    if limit is not None and len(inventory.matches) > limit:
        lines.append(f"... {len(inventory.matches) - limit} more entries "
                     "(full inventory in the JSON plan)")
    return "\n".join(lines)


def selection_summary(inventory: Inventory, policy: ProtectionPolicy) -> str:
    selected = inventory.selected()
    categories: dict[str, int] = {}
    total = 0
    for match in selected:
        categories[match.category] = categories.get(match.category, 0) + 1
        total += match.size_bytes
    parts = [f"{count} x {category}" for category, count in sorted(categories.items())]
    return (f"{len(selected)} item(s) selected ({human_size(total)}): "
            + (", ".join(parts) if parts else "nothing selected"))


def confirm_deletion(stream_in: TextIO, stream_out: TextIO) -> bool:
    """The explicit confirmation gate. Anything but the exact phrase aborts."""
    stream_out.write(
        "\nThis will act on the selected items. Type "
        f"{CONFIRMATION_PHRASE!r} to proceed, anything else to cancel: ")
    stream_out.flush()
    answer = stream_in.readline()
    if answer.strip() != CONFIRMATION_PHRASE:
        stream_out.write("Cancelled — no changes were made.\n")
        return False
    return True


def _parse_ranges(spec: str) -> list[int]:
    out: list[int] = []
    for part in spec.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            out.extend(range(int(lo), int(hi) + 1))
        else:
            out.append(int(part))
    return out


def interactive_review(inventory: Inventory, policy: ProtectionPolicy,
                       stream_in: TextIO | None = None,
                       stream_out: TextIO | None = None,
                       ) -> str:
    """Run the review loop. Returns 'proceed' or 'cancel'."""
    stream_in = stream_in or sys.stdin
    stream_out = stream_out or sys.stdout

    def emit(text: str) -> None:
        stream_out.write(text + "\n")
        stream_out.flush()

    emit(format_inventory(inventory, policy))
    emit("")
    emit(MENU_HELP)
    while True:
        stream_out.write("uninstall> ")
        stream_out.flush()
        line = stream_in.readline()
        if not line:
            return "cancel"
        command = line.strip().lower()
        if command in ("", "l"):
            emit(format_inventory(inventory, policy))
        elif command == "q":
            emit("Cancelled — nothing has been changed.")
            return "cancel"
        elif command == "p":
            emit(selection_summary(inventory, policy))
            preserved = [m for m in inventory.matches
                         if m.protected and m.category in PROTECTED_CATEGORIES]
            if preserved:
                emit(f"preserved entries left untouched: {len(preserved)} "
                     "(use --include-* options to change)")
            return "proceed"
        elif command in ("sa", "select all"):
            for match in inventory.matches:
                if policy.allows(match) and match.selectable and not match.ambiguous:
                    match.selected = True
            emit(selection_summary(inventory, policy))
        elif command in ("sn", "select none"):
            for match in inventory.matches:
                match.selected = False
            emit(selection_summary(inventory, policy))
        elif command.startswith("s ") or command.startswith("d "):
            selecting = command.startswith("s ")
            try:
                indices = _parse_ranges(command[2:])
            except ValueError:
                emit("could not parse item numbers")
                continue
            for index in indices:
                if 1 <= index <= len(inventory.matches):
                    match = inventory.matches[index - 1]
                    if selecting and not policy.allows(match):
                        emit(f"item {index} is preserved ({match.category}); "
                             "enable its category first")
                        continue
                    if selecting and not match.selectable:
                        emit(f"item {index} cannot be selected")
                        continue
                    match.selected = selecting
            emit(selection_summary(inventory, policy))
        elif command.startswith("cat "):
            pieces = command.split()
            if len(pieces) != 3 or pieces[2] not in ("on", "off"):
                emit("usage: cat <category> on|off")
                continue
            category, state = pieces[1], pieces[2] == "on"
            count = 0
            for match in inventory.matches:
                if match.category != category:
                    continue
                if state and not policy.allows(match):
                    continue
                match.selected = state and match.selectable
                count += 1
            emit(f"{count} item(s) in {category} set to "
                 f"{'selected' if state else 'unselected'}")
        elif command == "protect on":
            policy.enabled = set(PROTECTED_CATEGORIES)
            emit("ALL preserved categories enabled — their entries can now be "
                 "selected individually.")
        elif command == "protect off":
            policy.enabled = set()
            changed = policy.enforce_selection(inventory)
            emit(f"preserved categories locked again ({len(changed)} item(s) "
                 "deselected)")
        else:
            emit(MENU_HELP)
