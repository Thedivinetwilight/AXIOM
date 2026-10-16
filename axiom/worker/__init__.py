"""Process isolation between AXIOM and the Kronos runtime.

``KronosManager`` / ``ChildRuntime`` are imported lazily so that running the
child as ``python -m axiom.worker.child`` does not double-import its own module
(which would emit a runpy warning). ``from axiom.worker import KronosManager``
still works.
"""

from typing import Any

from axiom.worker.protocol import (
    REQUEST_KINDS,
    KronosRequest,
    KronosResponse,
    ProtocolError,
    decode_bars,
    decode_timestamps,
    encode_bars,
    encode_timestamps,
)

__all__ = [
    "REQUEST_KINDS",
    "ChildRuntime",
    "KronosManager",
    "KronosRequest",
    "KronosResponse",
    "ProtocolError",
    "child_main",
    "decode_bars",
    "decode_timestamps",
    "encode_bars",
    "encode_timestamps",
    "in_process_manager",
]

_LAZY = {
    "ChildRuntime": ("axiom.worker.child", "ChildRuntime"),
    "child_main": ("axiom.worker.child", "main"),
    "KronosManager": ("axiom.worker.manager", "KronosManager"),
    "in_process_manager": ("axiom.worker.manager", "in_process_manager"),
}


def __getattr__(name: str) -> Any:
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = target
    import importlib

    return getattr(importlib.import_module(module_name), attribute)


def __dir__() -> list[str]:
    return sorted(set(__all__) | set(globals()))
