"""Protected categories: preserved unless explicitly enabled.

Seven categories are preserved by default — trading history, logs,
configuration, credentials, source repositories, development folders and
research. They are not merely unselected: unless the operator passed the
category's own ``--include-*`` option (or enabled it interactively), the
selection stage force-deselects them and the deletion stage refuses them
even if a plan file claims otherwise. A stale or hand-edited plan cannot
silently destroy a trading database.
"""

from __future__ import annotations

from typing import Iterable

from uninstaller.constants import (
    PROTECTED_CATEGORIES,
    PROTECTED_CATEGORY_FLAGS,
)
from uninstaller.model import Inventory, Match


class ProtectionPolicy:
    """Which protected categories the operator has deliberately enabled."""

    def __init__(self, enabled: Iterable[str] | None = None) -> None:
        self.enabled: set[str] = set(enabled or ())
        unknown = self.enabled - PROTECTED_CATEGORIES
        if unknown:
            raise ValueError(f"not protected categories: {sorted(unknown)}")

    @classmethod
    def from_flags(cls, flags: dict[str, bool]) -> "ProtectionPolicy":
        """Build the policy from CLI flags.

        argparse exposes ``--include-logs`` as ``include_logs``, so both
        spellings are accepted; anything else would silently ignore the
        operator's explicit opt-in.
        """
        enabled = {category for flag, category in PROTECTED_CATEGORY_FLAGS.items()
                   if flags.get(flag) or flags.get(flag.replace("-", "_"))}
        return cls(enabled)

    def is_enabled(self, category: str) -> bool:
        return category in self.enabled

    def allows(self, match: Match) -> bool:
        if not match.protected:
            return True
        return match.category in self.enabled

    def enforce_selection(self, inventory: Inventory) -> list[Match]:
        """Force-deselect protected matches whose category is not enabled.

        Returns the list of matches that were deselected (for reporting).
        """
        changed: list[Match] = []
        for match in inventory.matches:
            if match.protected and match.selected and not self.allows(match):
                match.selected = False
                changed.append(match)
        return changed

    def describe(self) -> str:
        if not self.enabled:
            return ("preserving: trading history, logs, configuration, "
                    "credentials, source repositories, development folders, "
                    "research")
        return "preserving everything except enabled: " + ", ".join(sorted(self.enabled))
