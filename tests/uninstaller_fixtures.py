"""Fixtures shared by the uninstaller tests and the packaged-binary e2e.

The centrepiece is ``build_pe``: a minimal but genuinely well-formed PE32+
image carrying a real ``VS_VERSION_INFO`` resource. Tests need binaries whose
metadata the uninstaller can read the same way it reads a shipped Arena.exe —
crafting the resource properly (rather than stubbing the parser) is what makes
the metadata signal testable on Linux as well as on Windows.

No third-party packages and no compiler: pure ``struct``.
"""

from __future__ import annotations

import json
import os
import struct
from pathlib import Path

# ---------------------------------------------------------------------------
# VS_VERSION_INFO
# ---------------------------------------------------------------------------


def _align4(data: bytes) -> bytes:
    """Pad to a 4-byte boundary measured from the structure's own start."""
    return data + b"\0" * ((4 - len(data) % 4) % 4)


def _wstr(text: str) -> bytes:
    return text.encode("utf-16-le") + b"\0\0"


def _string_entry(key: str, value: str) -> bytes:
    body = _align4(struct.pack("<HHH", 0, len(value) + 1, 1) + _wstr(key))
    body += _wstr(value)
    return struct.pack("<HHH", len(body), len(value) + 1, 1) + body[6:]


def _string_table(entries: dict[str, str]) -> bytes:
    body = _align4(struct.pack("<HHH", 0, 0, 1) + _wstr("040904B0"))
    for key, value in entries.items():
        # Every structure is DWORD-aligned relative to the blob start; the
        # table itself starts aligned, so padding here is equivalent.
        body += _align4(_string_entry(key, value))
    return struct.pack("<HHH", len(body), 0, 1) + body[6:]


def _string_file_info(entries: dict[str, str]) -> bytes:
    table = _string_table(entries)
    body = _align4(struct.pack("<HHH", 0, 0, 1) + _wstr("StringFileInfo"))
    body += _align4(table)
    return struct.pack("<HHH", len(body), 0, 1) + body[6:]


def _fixed_file_info(version: tuple[int, int, int, int]) -> bytes:
    major, minor, patch, build = version
    ms = (major << 16) | minor
    ls = (patch << 16) | build
    return struct.pack(
        "<13I", 0xFEEF04BD, 0x00010000, ms, ls, ms, ls,
        0x3F, 0, 0x40004, 0x1, 0, 0, 0)


def build_version_blob(entries: dict[str, str],
                       version: tuple[int, int, int, int] = (3, 2, 4, 0)) -> bytes:
    """A complete VS_VERSION_INFO resource blob."""
    body = _align4(struct.pack("<HHH", 0, 52, 0) + _wstr("VS_VERSION_INFO"))
    body += _fixed_file_info(version)
    body += _string_file_info(entries)
    return struct.pack("<HHH", len(body), 52, 0) + body[6:]


# ---------------------------------------------------------------------------
# Minimal PE32+ image with the version resource in .rsrc
# ---------------------------------------------------------------------------

_SECTION_RVA = 0x1000


def _resource_directory(blob: bytes) -> bytes:
    """Build the three-level resource tree around one RT_VERSION blob."""
    blob_off = 0x58
    rsrc = bytearray(blob_off)
    # level 0: type directory
    struct.pack_into("<IIHHHH", rsrc, 0x00, 0, 0, 0, 0, 0, 1)
    struct.pack_into("<II", rsrc, 0x10, 16, 0x80000000 | 0x18)  # RT_VERSION
    # level 1: name directory
    struct.pack_into("<IIHHHH", rsrc, 0x18, 0, 0, 0, 0, 0, 1)
    struct.pack_into("<II", rsrc, 0x28, 1, 0x80000000 | 0x30)
    # level 2: language directory
    struct.pack_into("<IIHHHH", rsrc, 0x30, 0, 0, 0, 0, 0, 1)
    struct.pack_into("<II", rsrc, 0x40, 1033, 0x48)
    # data entry points at the blob RVA
    struct.pack_into("<IIII", rsrc, 0x48, _SECTION_RVA + blob_off, len(blob),
                     1200, 0)
    rsrc += blob
    return bytes(rsrc)


def build_pe(path: Path, *, product: str = "AXIOM Arena",
             company: str = "Thedivinetwilight",
             internal: str = "Arena",
             original: str = "Arena.exe",
             version: tuple[int, int, int, int] = (3, 2, 4, 0),
             description: str = "AXIOM Arena - Quantitative Research & "
                                "Paper Trading Platform",
             padding: int = 512) -> Path:
    """Write a fake Windows binary whose metadata says exactly what we want."""
    blob = build_version_blob({
        "CompanyName": company,
        "FileDescription": description,
        "FileVersion": ".".join(str(part) for part in version),
        "InternalName": internal,
        "OriginalFilename": original,
        "ProductName": product,
        "ProductVersion": ".".join(str(part) for part in version),
    }, version)
    rsrc = _resource_directory(blob)
    raw_size = len(rsrc) + padding
    raw = rsrc + b"\0" * padding

    dos = bytearray(0x80)
    dos[0:2] = b"MZ"
    struct.pack_into("<I", dos, 0x3C, 0x80)

    coff = struct.pack("<HHIIIHH", 0x8664, 1, 0, 0, 0, 0xF0, 0x0022)
    opt = bytearray(0xF0)
    struct.pack_into("<H", opt, 0, 0x20B)          # PE32+
    struct.pack_into("<I", opt, 16, 0)             # AddressOfEntryPoint
    struct.pack_into("<Q", opt, 24, 0x140000000)   # ImageBase
    struct.pack_into("<I", opt, 32, 0x1000)        # SectionAlignment
    struct.pack_into("<I", opt, 36, 0x200)         # FileAlignment
    struct.pack_into("<I", opt, 56, _SECTION_RVA + len(rsrc) + 0x1000)  # SizeOfImage
    struct.pack_into("<I", opt, 60, 0x200)         # SizeOfHeaders
    struct.pack_into("<H", opt, 68, 3)             # Subsystem: console
    struct.pack_into("<I", opt, 108, 16)           # NumberOfRvaAndSizes
    struct.pack_into("<II", opt, 112 + 2 * 8, _SECTION_RVA, len(rsrc))

    section = struct.pack(
        "<8sIIIIIIHHI", b".rsrc\0\0\0", len(rsrc), _SECTION_RVA, raw_size,
        0x200, 0, 0, 0, 0, 0x40000040)

    image = bytes(dos) + b"PE\0\0" + coff + bytes(opt) + section
    image += b"\0" * (0x200 - len(image))
    image += raw
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(image)
    return path


# ---------------------------------------------------------------------------
# Fixture trees
# ---------------------------------------------------------------------------


def make_fake_install(root: Path, *, with_data: bool = True,
                      product: str = "AXIOM Arena") -> dict[str, Path]:
    """An installation tree shaped like the Inno Setup layout we ship."""
    root.mkdir(parents=True, exist_ok=True)
    files = {
        "exe": build_pe(root / "Arena.exe", product=product,
                        internal="Arena", original="Arena.exe"),
        "uninstaller": build_pe(root / "unins000.exe", product=product,
                                internal="unins000", original="unins000.exe"),
        "readme": root / "PAPER_ACCOUNT_RECOVERY.md",
    }
    files["readme"].write_text("# Recovery\n\nYour data is safe.\n",
                               encoding="utf-8")
    (root / "unins000.dat").write_bytes(b"inno uninstall data")
    (root / ".env").write_text("setup\nMODE=PAPER\nBROKER=LOCAL_PAPER\n"
                              "LIVE_TRADING=false\nUPSTOX_API_KEY=SECRET-TOKEN-VALUE\n",
                              encoding="utf-8")
    if with_data:
        (root / "data" / "history").mkdir(parents=True, exist_ok=True)
        (root / "data" / "history" / "DEMO.csv").write_text(
            "ts,open,high,low,close,volume\n", encoding="utf-8")
        (root / "data" / "cache").mkdir(parents=True, exist_ok=True)
        (root / "data" / "cache" / "candles.bin").write_bytes(b"\0" * 2048)
        (root / "logs").mkdir(parents=True, exist_ok=True)
        (root / "logs" / "bot.jsonl").write_text('{"event": "start"}\n',
                                                 encoding="utf-8")
        (root / "trading_bot.db").write_bytes(b"SQLite format 3\0" + b"\0" * 512)
        (root / "research").mkdir(parents=True, exist_ok=True)
        (root / "research" / "notes.md").write_text("my research\n",
                                                    encoding="utf-8")
        (root / "__pycache__").mkdir(parents=True, exist_ok=True)
        (root / "__pycache__" / "app.cpython-311.pyc").write_bytes(b"\0" * 64)
    return files


def make_fake_home(home: Path) -> None:
    """The per-user runtime home (what app/runtime.py creates)."""
    for child in ("data/history", "data/cache", "logs", "results", "backups"):
        (home / child).mkdir(parents=True, exist_ok=True)
    (home / ".env").write_text("MODE=PAPER\nUPSTOX_ACCESS_TOKEN=live-secret-123\n",
                               encoding="utf-8")
    (home / "trading_bot.db").write_bytes(b"SQLite format 3\0" + b"\0" * 256)
    (home / "data" / "history" / "AAA.csv").write_text("ts,close\n",
                                                       encoding="utf-8")
    (home / "logs" / "bot.jsonl").write_text('{"event": "tick"}\n',
                                             encoding="utf-8")
    (home / "instance.lock").write_bytes(b"")
    (home / "instance.json").write_text(json.dumps({"pid": 1}), encoding="utf-8")


def make_false_positives(root: Path) -> dict[str, Path]:
    """Look-alikes that must NEVER be classified as Arena-owned."""
    root.mkdir(parents=True, exist_ok=True)
    created = {}
    created["note"] = root / "AXIOM-notes.txt"
    created["note"].write_text("my notes about the axiom\n", encoding="utf-8")
    created["game"] = root / "Arena Games"
    created["game"].mkdir()
    (created["game"] / "readme.txt").write_text("unrelated game\n",
                                                encoding="utf-8")
    created["csv"] = root / "arena_tournament_data.csv"
    created["csv"].write_text("a,b\n1,2\n", encoding="utf-8")
    created["other_inno"] = build_pe(
        root / "unins000.exe", product="Notepad Replacement",
        company="Some Other Vendor", internal="unins000",
        original="unins000.exe")
    created["other_uninstall"] = build_pe(
        root / "ArenaTool.exe", product="Arena Tool", company="Arena Inc",
        internal="ArenaTool", original="ArenaTool.exe")
    return created


# ---------------------------------------------------------------------------
# A complete fake machine: paths, environment and registry
# ---------------------------------------------------------------------------

from dataclasses import dataclass, field as _field  # noqa: E402


@dataclass
class World:
    """Every location a fake Windows machine would have, inside a tmpdir."""

    root: Path
    program_files: Path
    local_appdata: Path
    roaming_appdata: Path
    user_profile: Path
    public: Path
    temp: Path
    downloads: Path
    tool_home: Path
    desktop: Path
    start_menu: Path
    env: dict[str, str] = _field(default_factory=dict)

    @property
    def install_root(self) -> Path:
        return self.program_files / "AXIOM"

    @property
    def runtime_home(self) -> Path:
        return self.local_appdata / "Arena"


def make_world(tmp: Path, *, install: bool = True, home: bool = True,
               downloads: bool = True, false_positives: bool = True) -> World:
    root = tmp / "machine"
    world = World(
        root=root,
        program_files=root / "ProgramFiles",
        local_appdata=root / "LocalAppData",
        roaming_appdata=root / "Roaming",
        user_profile=root / "User",
        public=root / "Public",
        temp=root / "Temp",
        downloads=root / "Downloads",
        tool_home=root / "toolhome",
        desktop=root / "User" / "Desktop",
        start_menu=root / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs",
    )
    for path in (world.program_files, world.local_appdata, world.roaming_appdata,
                 world.user_profile, world.public, world.temp, world.downloads,
                 world.tool_home, world.desktop, world.start_menu):
        path.mkdir(parents=True, exist_ok=True)
    if install:
        make_fake_install(world.install_root)
    if home:
        make_fake_home(world.runtime_home)
    if downloads:
        build_pe(world.downloads / "AXIOM-Setup-3.2.4.exe",
                 product="AXIOM Arena", internal="AXIOM-Setup",
                 original="AXIOM-Setup-3.2.4.exe")
        (world.downloads / "arena-notes.txt").write_text("notes\n",
                                                         encoding="utf-8")
    if false_positives:
        make_false_positives(root / "Games")
    world.env = {
        "ProgramFiles": str(world.program_files),
        "ProgramFiles(x86)": str(world.program_files),
        "ProgramW6432": str(world.program_files),
        "LOCALAPPDATA": str(world.local_appdata),
        "APPDATA": str(world.roaming_appdata),
        "USERPROFILE": str(world.user_profile),
        "PUBLIC": str(world.public),
        "TEMP": str(world.temp),
        "TMP": str(world.temp),
        "ARENA_UNINSTALL_HOME": str(world.tool_home),
    }
    return world


def merged_env(world: World) -> dict[str, str]:
    """The current environment with the fixture's locations applied.

    Windows treats environment names case-insensitively, so replacing a value
    must REMOVE any case-variant of the key first — otherwise the real
    ``PROGRAMFILES`` can shadow the fixture's ``ProgramFiles`` and the tool
    silently scans the wrong machine.
    """
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        env[key] = value
    for key, value in world.env.items():
        for existing in [k for k in env if k.upper() == key.upper()]:
            del env[existing]
        env[key] = value
    return env


def fake_platform(world: World, *, entries=None, admin: bool = False,
                  running=None):
    """A Platform wired to fakes; no Windows APIs are touched."""
    from uninstaller.winapi import Platform, RegistryProvider

    class FakeRegistry(RegistryProvider):
        def __init__(self, items):
            self.items = list(items or [])
            self.removed: list[dict] = []

        def entries(self):
            return list(self.items), []

        def remove_registration(self, payload):
            self.removed.append(payload)
            return True, "registration removed (fake)"

    registry = FakeRegistry(entries)
    return Platform(registry=registry, environ=dict(world.env),
                    force_admin=admin, running_override=running)
