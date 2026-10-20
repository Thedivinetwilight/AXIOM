"""Arena Deep Uninstaller — a standalone Windows removal and cleanup tool.

This package depends on the Python standard library ONLY and imports nothing
from the Arena application. That is a hard requirement, not a style choice:
the uninstaller has to work when Arena is broken, half-removed, or gone, and
it ships as its own executable built from this package alone.

Modules:

``constants``   product identity tokens and category definitions
``model``       Match / Inventory / ActionReport data model
``signals``     evidence gathering (registry, metadata, markers, names)
``classify``    evidence combination and tree-content rules
``scanner``     registry, known-path and drive scanning (reparse-safe)
``duplicates``  byte-identical artefact grouping (inside ``scanner``)
``protect``     the seven preserved categories and their opt-in policy
``quarantine``  reversible quarantine with manifest and restore
``deletion``    guarded execution with failure reporting
``logbook``     secret-redacted cleanup log
``review``      the full inventory screen and confirmation gate
``engine``      orchestration: scan -> review -> confirm -> execute
``cli``         command-line interface
``pe``          pure-Python PE version-resource reader
``winapi``      Windows services behind an injectable Platform interface
"""

from __future__ import annotations

__version__ = "3.2.4"
__all__ = ["__version__"]
