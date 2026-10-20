"""Windows platform services behind a small, injectable interface.

Every operating-system touchpoint of the uninstaller lives in this module:
drive enumeration, reparse-point detection, registry access, elevation and
deferred self-deletion. The rest of the package talks to a ``Platform``
instance, so the test suite drives the exact same logic on any OS with
deterministic fakes instead of a real Windows box.

Safety rules encoded here:

* ``fixed_drives`` returns only fixed, ready volumes (``GetDriveType ==
  DRIVE_FIXED``) — never network shares or removable media;
* ``is_reparse_point`` is how the scanner refuses to follow junctions and
  symlinks into unrelated locations;
* ``defer_delete`` handles "delete the file that is currently running" by
  renaming it and scheduling the real deletion for the next reboot, instead
  of failing or, worse, corrupting the running image.
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence




@dataclass
class UninstallEntry:
    """One Add/Remove Programs registration."""

    hive: str                 # "HKCU" | "HKLM"
    key_path: str             # e.g. SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\...
    display_name: str = ""
    publisher: str = ""
    install_location: str = ""
    uninstall_string: str = ""
    display_icon: str = ""
    quiet_uninstall_string: str = ""
    estimated_size_kb: int = 0

    @property
    def key_id(self) -> str:
        return f"{self.hive}\\{self.key_path}"


class RegistryProvider:
    """Enumerate (and remove) uninstall registrations.

    The real implementation uses ``winreg``; tests substitute fakes. Reading
    never raises: an unreadable key becomes a reported issue, not a crash.
    """

    def entries(self) -> tuple[list[UninstallEntry], list[tuple[str, str]]]:
        """Return (entries, issues). issues are (path, detail) strings."""
        return [], []

    def remove_registration(self, payload: dict) -> tuple[bool, str]:
        """Remove one registration identified by its inventory payload."""
        raise NotImplementedError("registry removal not available")


class WinRegistryProvider(RegistryProvider):
    """winreg-backed provider (Windows only)."""

    def __init__(self) -> None:
        import winreg  # noqa: F401  (import here: Windows only)
        self._winreg = winreg

    def _iter_keys(self):
        winreg = self._winreg
        views = (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY)
        for hive_name, hive in (("HKCU", winreg.HKEY_CURRENT_USER),
                                ("HKLM", winreg.HKEY_LOCAL_MACHINE)):
            for view in views:
                yield hive_name, hive, view

    def entries(self) -> tuple[list[UninstallEntry], list[tuple[str, str]]]:
        winreg = self._winreg
        base = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
        found: list[UninstallEntry] = []
        issues: list[tuple[str, str]] = []
        for hive_name, hive, view in self._iter_keys():
            try:
                root = winreg.OpenKey(hive, base, 0,
                                      winreg.KEY_READ | view)
            except OSError as exc:
                issues.append((f"{hive_name}\\{base}",
                               f"cannot open uninstall key: {exc}"))
                continue
            try:
                index = 0
                while True:
                    try:
                        sub_name = winreg.EnumKey(root, index)
                    except OSError:
                        break
                    index += 1
                    entry = self._read_entry(hive_name, hive, base, sub_name, view)
                    if entry is not None:
                        found.append(entry)
            finally:
                winreg.CloseKey(root)
        return found, issues

    def _read_entry(self, hive_name, hive, base, sub_name, view) -> UninstallEntry | None:
        winreg = self._winreg
        path = f"{base}\\{sub_name}"
        try:
            key = winreg.OpenKey(hive, path, 0, winreg.KEY_READ | view)
        except OSError:
            return None
        try:
            def value(name: str) -> str:
                try:
                    data, _ = winreg.QueryValueEx(key, name)
                    return str(data) if data is not None else ""
                except OSError:
                    return ""

            def dword(name: str) -> int:
                try:
                    data, _ = winreg.QueryValueEx(key, name)
                    return int(data)
                except (OSError, TypeError, ValueError):
                    return 0

            return UninstallEntry(
                hive=hive_name,
                key_path=path,
                display_name=value("DisplayName"),
                publisher=value("Publisher"),
                install_location=value("InstallLocation").strip().strip('"'),
                uninstall_string=value("UninstallString"),
                display_icon=value("DisplayIcon"),
                quiet_uninstall_string=value("QuietUninstallString"),
                estimated_size_kb=dword("EstimatedSize"),
            )
        finally:
            winreg.CloseKey(key)

    def remove_registration(self, payload: dict) -> tuple[bool, str]:
        entry = UninstallEntry(
            hive=str(payload.get("hive") or payload.get("key_id", "").split("\\")[0]),
            key_path=str(payload.get("key_path") or
                         payload.get("key_id", "").split("\\", 1)[-1]),
        )
        return self._remove_entry(entry)

    def _remove_entry(self, entry: UninstallEntry) -> tuple[bool, str]:
        winreg = self._winreg
        hive = (winreg.HKEY_CURRENT_USER if entry.hive == "HKCU"
                else winreg.HKEY_LOCAL_MACHINE)
        try:
            winreg.DeleteKey(hive, entry.key_path)
            return True, "registration removed"
        except OSError as exc:
            # Values may make DeleteKey fail; try deleting values first.
            try:
                key = winreg.OpenKey(hive, entry.key_path, 0,
                                     winreg.KEY_ALL_ACCESS)
                try:
                    while True:
                        try:
                            name = winreg.EnumValue(key, 0)[0]
                        except OSError:
                            break
                        winreg.DeleteValue(key, name)
                finally:
                    winreg.CloseKey(key)
                winreg.DeleteKey(hive, entry.key_path)
                return True, "registration removed"
            except OSError as exc2:
                return False, f"registry removal failed: {exc2 or exc}"


@dataclass
class Platform:
    """All OS services the uninstaller needs, in one injectable place."""

    registry: RegistryProvider = field(default_factory=RegistryProvider)
    environ: dict[str, str] = field(default_factory=lambda: dict(os.environ))
    force_admin: bool | None = None       # test override
    running_override: Sequence[str] | None = None  # test override

    # -- identity -----------------------------------------------------------

    @property
    def is_windows(self) -> bool:
        return os.name == "nt"

    def self_paths(self) -> list[str]:
        """Paths of THIS tool that must never be deleted naively."""
        paths = []
        if getattr(sys, "frozen", False):
            paths.append(str(Path(sys.executable).resolve()))
            meipass = getattr(sys, "_MEIPASS", None)
            if meipass:
                paths.append(str(Path(meipass).resolve()))
        return paths

    def is_self_path(self, path: str | Path) -> bool:
        try:
            resolved = str(Path(path).resolve())
        except OSError:
            resolved = str(path)
        return resolved in self.self_paths()

    # -- drives and filesystem ---------------------------------------------

    def fixed_drives(self) -> list[str]:
        """Accessible local fixed drive roots, e.g. ``['C:\\\\']``."""
        if not self.is_windows:
            return []
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetLogicalDrives.restype = wintypes.DWORD
        kernel32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetDriveTypeW.restype = wintypes.UINT
        drives: list[str] = []
        try:
            bitmask = kernel32.GetLogicalDrives()
        except (AttributeError, OSError):  # pragma: no cover - defensive
            return []
        for i in range(26):
            if not bitmask & (1 << i):
                continue
            root = f"{chr(ord('A') + i)}:\\"
            try:
                # DRIVE_FIXED == 3; DRIVE_RAMDISK (6) treated as fixed too
                # because a RAM disk is still a local volume the user owns.
                dtype = kernel32.GetDriveTypeW(root)
            except (AttributeError, OSError):  # pragma: no cover
                continue
            if dtype in (3, 6) and os.path.isdir(root):
                drives.append(root)
        return drives

    def is_reparse_point(self, path: Path) -> bool:
        """True for junctions, symlinks and other reparse points.

        The scanner calls this before descending and never follows a True
        result: following a junction can walk into unrelated (or endlessly
        recursive) locations.
        """
        try:
            st = os.lstat(path)
        except OSError:
            return False
        if stat.S_ISLNK(st.st_mode):
            return True
        attrs = getattr(st, "st_file_attributes", 0)
        return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))

    def list_running_products(self) -> list[str]:
        """Names of running Arena-family processes (lowercased)."""
        if self.running_override is not None:
            return list(self.running_override)
        if not self.is_windows:
            return []
        import ctypes
        from ctypes import wintypes

        TH32CS_SNAPPROCESS = 0x2
        class PROCESSENTRY32(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_char * 260),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # A snapshot handle is pointer-sized: leaving the default c_int
        # restype truncates it on 64-bit Windows and every later call fails.
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD,
                                                      wintypes.DWORD]
        kernel32.Process32First.argtypes = [wintypes.HANDLE,
                                            ctypes.POINTER(PROCESSENTRY32)]
        kernel32.Process32Next.argtypes = [wintypes.HANDLE,
                                           ctypes.POINTER(PROCESSENTRY32)]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        invalid = ctypes.c_void_p(-1).value
        if not snap or snap == invalid:
            return []
        names: list[str] = []
        try:
            entry = PROCESSENTRY32()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
            if kernel32.Process32First(snap, ctypes.byref(entry)):
                while True:
                    raw = bytes(entry.szExeFile).split(b"\0", 1)[0]
                    try:
                        names.append(raw.decode("mbcs", errors="replace").lower())
                    except LookupError:  # pragma: no cover - exotic code page
                        names.append(raw.decode("utf-8", errors="replace").lower())
                    if not kernel32.Process32Next(snap, ctypes.byref(entry)):
                        break
        finally:
            kernel32.CloseHandle(snap)
        wanted = {"arena.exe", "axiom.exe", "axiom-kronos.exe"}
        return sorted({n for n in names if n in wanted})

    # -- privilege ---------------------------------------------------------

    def is_admin(self) -> bool:
        if self.force_admin is not None:
            return self.force_admin
        if not self.is_windows:
            return os.geteuid() == 0 if hasattr(os, "geteuid") else False
        try:
            import ctypes
            shell32 = ctypes.WinDLL("shell32", use_last_error=True)
            shell32.IsUserAnAdmin.restype = ctypes.c_int
            return bool(shell32.IsUserAnAdmin())
        except (AttributeError, OSError):  # pragma: no cover - defensive
            return False

    def relaunch_elevated(self, argv: Sequence[str]) -> tuple[bool, str]:
        """Relaunch this exact tool elevated. Only called when required."""
        if not self.is_windows:
            return False, "elevation relaunch is only available on Windows"
        import ctypes
        from ctypes import wintypes
        executable = sys.executable
        params = " ".join(_quote(a) for a in argv)
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        shell32.ShellExecuteW.argtypes = [wintypes.HWND, wintypes.LPCWSTR,
                                          wintypes.LPCWSTR, wintypes.LPCWSTR,
                                          wintypes.LPCWSTR, ctypes.c_int]
        shell32.ShellExecuteW.restype = ctypes.c_void_p
        try:
            rc = shell32.ShellExecuteW(None, "runas", executable, params,
                                       None, 1)
        except (AttributeError, OSError) as exc:  # pragma: no cover
            return False, f"ShellExecuteW failed: {exc}"
        rc_value = rc if isinstance(rc, int) else -1
        if rc_value > 32:
            return True, "relaunched elevated"
        return False, f"elevation was declined or failed (code {rc})"

    # -- deferred self deletion -------------------------------------------

    def defer_delete(self, path: Path) -> tuple[bool, str]:
        """Handle deleting a file that may be the running image.

        Renames the file and schedules its removal at the next reboot
        (MoveFileEx MOVEFILE_DELAY_UNTIL_REBOOT) so the running process is
        never disturbed and the file does eventually go away.
        """
        if not self.is_windows:
            return False, "deferred deletion is only available on Windows"
        import ctypes

        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.MoveFileExW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR,
                                         wintypes.DWORD]
        kernel32.MoveFileExW.restype = wintypes.BOOL
        MOVEFILE_DELAY_UNTIL_REBOOT = 0x4
        target = path.with_name(path.name + ".arena-uninstall-pending")
        try:
            os.replace(path, target)
        except OSError as exc:
            target = path
            detail_prefix = f"rename failed ({exc}); "
        else:
            detail_prefix = ""
        ok = kernel32.MoveFileExW(None, str(target),
                                  MOVEFILE_DELAY_UNTIL_REBOOT)
        if ok:
            return True, detail_prefix + "scheduled for deletion at next reboot"
        return False, detail_prefix + "MoveFileExW(DELAY_UNTIL_REBOOT) failed"

    def shell_quote(self, arg: str) -> str:
        return _quote(arg)


def _quote(arg: str) -> str:
    if not arg or any(ch in arg for ch in ' \t"'):
        return '"' + arg.replace('"', '\\"') + '"'
    return arg


# ---------------------------------------------------------------------------
# Well-known locations (env-derived so tests can point them at a sandbox)
# ---------------------------------------------------------------------------

def env_get(environ: dict[str, str], name: str) -> str:
    """Read an environment variable regardless of key casing.

    Windows environment blocks are case-insensitive, so a merged dict can
    legitimately contain both ``PROGRAMFILES`` and ``ProgramFiles``. Picking
    the wrong one silently changes where the uninstaller looks.
    """
    if name in environ:
        return environ[name]
    wanted = name.upper()
    for key, value in environ.items():
        if key.upper() == wanted:
            return value
    return ""


@dataclass
class KnownPaths:
    """Product-relevant well-known directories on this machine."""

    program_files: list[Path] = field(default_factory=list)
    local_appdata: Path | None = None
    roaming_appdata: Path | None = None
    user_profile: Path | None = None
    public: Path | None = None
    temp: Path | None = None

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "KnownPaths":
        env = environ if environ is not None else dict(os.environ)
        program_files = []
        for var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
            value = env_get(env, var)
            if value:
                path = Path(value)
                if path not in program_files:
                    program_files.append(path)
        local = env_get(env, "LOCALAPPDATA")
        roaming = env_get(env, "APPDATA")
        profile = env_get(env, "USERPROFILE")
        public = env_get(env, "PUBLIC")
        temp = env_get(env, "TEMP") or env_get(env, "TMP")
        return cls(
            program_files=program_files,
            local_appdata=Path(local) if local else None,
            roaming_appdata=Path(roaming) if roaming else None,
            user_profile=Path(profile) if profile else None,
            public=Path(public) if public else None,
            temp=Path(temp) if temp else None,
        )

    def install_candidates(self) -> list[Path]:
        """Directories a real Arena/AXIOM install or per-user home may occupy.

        Presence of one of these paths is a STRONG-ish signal (known-app-path)
        but its CONTENTS still get classified item by item — the home
        directory in particular is mostly protected user data.
        """
        found: list[Path] = []
        for base in self.program_files:
            for name in ("AXIOM", "Arena"):
                found.append(base / name)
        if self.local_appdata:
            found.append(self.local_appdata / "Programs" / "AXIOM")
            found.append(self.local_appdata / "Programs" / "Arena")
            found.append(self.local_appdata / "AXIOM")
            found.append(self.local_appdata / "Arena")
        return found

    def models_cache(self) -> Path | None:
        if self.local_appdata:
            return self.local_appdata / "AXIOM" / "models"
        return None

    def start_menu_group(self) -> Path | None:
        if self.roaming_appdata:
            return (self.roaming_appdata / "Microsoft" / "Windows"
                    / "Start Menu" / "Programs" / "AXIOM Arena")
        return None

    def desktops(self) -> list[Path]:
        found = []
        if self.user_profile:
            found.append(self.user_profile / "Desktop")
        if self.public:
            found.append(self.public / "Desktop")
        return found


def which_python() -> str:
    return sys.executable


def copy_tool(src: Path, dst: Path) -> None:
    shutil.copy2(src, dst)
