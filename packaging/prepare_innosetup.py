#!/usr/bin/env python3
"""Prepare a VERIFIED Inno Setup 6 compiler for the Windows installer build.

Why this script exists
----------------------
The Windows runner image already ships Inno Setup (6.7.1 at the time of
writing). A pinned install such as::

    choco install innosetup --version=6.2.2 -y

then REFUSES to run: chocolatey will not downgrade a package that is already
installed at a newer version, and it exits non-zero. The whole workflow
therefore died in the environment-setup step — before a single test ran, and
before any executable was built. Nothing about the product was wrong; the
environment setup was simply written for a machine that did not have
Inno Setup yet.

The fix is an explicit, two-step resolution:

1. **DETECT** an already installed compiler and USE it. Every known install
   location is searched, and the candidate must then PROVE it works: the
   script compiles a tiny probe script that uses the same features the real
   installer script needs (a ``/D`` define and the ``x64compatible``
   architecture identifier). A compiler that cannot compile the probe is not
   used.
2. **INSTALL** a known-good version when no usable compiler is present. The
   official installer is downloaded and run silently, then the compiler is
   detected and probed again. Chocolatey is tried first, WITHOUT a version
   pin, because an unpinned install is compatible with whatever the image
   already provides.

Every failure is fatal. The script never pretends to have found a compiler,
never downgrades an existing installation, and never leaves the workflow in
a state where a later step quietly succeeds with the wrong tools.

Usage::

    python packaging/prepare_innosetup.py                 # resolve and report
    python packaging/prepare_innosetup.py --github-env "$GITHUB_ENV"

On success the resolved compiler is written to ``--github-env`` as
``ISCC_PATH=<windows path>`` so later steps use exactly the compiler that was
verified here.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# The installer script uses ``ArchitecturesAllowed=x64compatible`` and a
# ``/D`` define. Both need Inno Setup 6.3 or newer.
MINIMUM_VERSION = (6, 3, 0)

# The version this repository asks for when it has to install one. The
# download is verified before it is executed and the result is probed
# afterwards, so a newer "latest" release is acceptable too.
INTENDED_VERSION = "6.4.3"

# Official Inno Setup downloads. The version-specific URL is tried first,
# then the "latest 6.x" URL.
DOWNLOAD_URLS = (
    f"https://jrsoftware.org/download.php/is-{INTENDED_VERSION}.exe",
    "https://jrsoftware.org/download.php/is.exe",
)

PROBE_SCRIPT = """\
; Probe script: proves this compiler understands the two features the real
; installer script relies on - a /D define and x64compatible.
#ifndef ProbeValue
  #error "ProbeValue must be defined"
#endif
[Setup]
AppName=Arena Probe
AppVersion={#ProbeValue}
DefaultDirName={autopf}\\ArenaProbe
OutputBaseFilename=ArenaProbe
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
"""


def _env_path(name: str) -> Path | None:
    """A directory from the environment, or None."""
    raw = (os.environ.get(name) or "").strip()
    return Path(raw) if raw else None


def candidate_iscc_paths() -> list[Path]:
    """Every place a usable ISCC.exe is looked for, most likely first.

    The runner image installs Inno Setup under ``Program Files (x86)``; a
    per-user install and chocolatey's shim directory are checked as well.
    ``ISCC_PATH`` always wins, because the workflow may point at a specific
    compiler.
    """
    candidates: list[Path] = []

    explicit = (os.environ.get("ISCC_PATH") or "").strip()
    if explicit:
        candidates.append(Path(explicit))

    roots: list[Path] = []
    for name in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA",
                 "ProgramData"):
        base = _env_path(name)
        if base is None:
            continue
        if name == "ProgramData":
            roots.append(base / "chocolatey" / "bin")
        elif name == "LOCALAPPDATA":
            roots.append(base / "Programs")
        else:
            roots.append(base)
    roots.append(Path("C:/Program Files (x86)"))
    roots.append(Path("C:/Program Files"))
    roots.append(Path("C:/ProgramData/chocolatey/bin"))

    for root in roots:
        candidates.append(root / "Inno Setup 6" / "ISCC.exe")
        candidates.append(root / "ISCC.exe")

    seen: set[str] = set()
    ordered: list[Path] = []
    for candidate in candidates:
        key = str(candidate).lower()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(candidate)
    return ordered


def parse_version(text: str) -> tuple[int, ...] | None:
    """Read a version like ``6.7.1`` or ``6.7.1.0`` out of arbitrary text."""
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text or "")
    if not match:
        return None
    parts = [int(part) for part in match.groups() if part is not None]
    return tuple(parts)


def version_at_least(version: tuple[int, ...] | None,
                      minimum: tuple[int, ...]) -> bool:
    """True when ``version`` is at least ``minimum`` (unknown is not)."""
    if not version:
        return False
    padded = tuple(list(version) + [0] * (len(minimum) - len(version)))
    return padded[:len(minimum)] >= minimum


def iscc_version(compiler: Path) -> tuple[int, ...] | None:
    """The compiler's own version, read from the executable.

    The version resource is the authoritative source and does not run the
    compiler, so it cannot pop a dialog on a build machine.
    """
    if os.name != "nt":
        return None
    escaped = str(compiler).replace("'", "''")
    command = (
        "$v = (Get-Item -LiteralPath '" + escaped + "').VersionInfo.ProductVersion; "
        "if ($v) { Write-Output $v }"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, timeout=120, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return parse_version(result.stdout)


def probe_iscc(compiler: Path) -> tuple[bool, str]:
    """Prove the compiler really works, by compiling a tiny script.

    A compiler that exists but cannot compile the features the installer
    script uses is not a compiler this build can use.
    """
    with tempfile.TemporaryDirectory(prefix="arena-iscc-probe-") as workdir:
        script = Path(workdir) / "probe.iss"
        script.write_text(PROBE_SCRIPT, encoding="utf-8")
        output = Path(workdir) / "out"
        output.mkdir(parents=True, exist_ok=True)
        try:
            result = subprocess.run(
                [str(compiler), str(script), f"/DProbeValue={INTENDED_VERSION}",
                 f"/O{output}"],
                capture_output=True, text=True, timeout=600, check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"the compiler could not be started: {exc}"
        if result.returncode != 0:
            tail = ((result.stdout or "") + (result.stderr or ""))[-600:]
            return False, f"the probe script did not compile: {tail.strip()}"
        produced = list(output.glob("*.exe"))
        if not produced:
            return False, "the probe compiled but produced no executable"
        return True, f"probe compiled {produced[0].name}"


def detect_iscc() -> tuple[Path | None, str]:
    """Find a compiler that passes the probe."""
    reasons: list[str] = []
    for candidate in candidate_iscc_paths():
        try:
            if not candidate.is_file():
                continue
        except OSError:
            continue
        version = iscc_version(candidate)
        if version is not None and not version_at_least(version, MINIMUM_VERSION):
            reasons.append(
                f"{candidate} is version "
                f"{'.'.join(str(part) for part in version)}, older than the "
                f"required {'.'.join(str(part) for part in MINIMUM_VERSION)}"
            )
            continue
        ok, detail = probe_iscc(candidate)
        if ok:
            shown = (".".join(str(part) for part in version) if version
                     else "unknown version")
            return candidate, f"using the installed Inno Setup {shown} ({detail})"
        reasons.append(f"{candidate}: {detail}")
    if reasons:
        return None, "; ".join(reasons)
    return None, "no ISCC.exe was found in any known location"


def _run(command: list[str], timeout: int = 900) -> tuple[int, str]:
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, f"{type(exc).__name__}: {exc}"
    return result.returncode, ((result.stdout or "") + (result.stderr or ""))[-2000:]


def install_with_chocolatey() -> tuple[bool, str]:
    """Install Inno Setup WITHOUT pinning a version.

    A pinned version is exactly what breaks on a runner that already has a
    newer one: chocolatey refuses to downgrade and exits non-zero.
    """
    if not shutil_which("choco"):
        return False, "chocolatey is not available"
    code, output = _run(["choco", "install", "innosetup", "--yes", "--no-progress"])
    if code != 0:
        return False, f"choco install failed ({code}): {output.strip()}"
    return True, "chocolatey installed Inno Setup"


def shutil_which(name: str) -> str | None:
    """A tiny ``shutil.which`` wrapper (kept separate for testability)."""
    import shutil

    return shutil.which(name)


def download_inno_setup(target: Path) -> tuple[bool, str]:
    """Download the official installer and check it before running it."""
    import urllib.request

    last_error = ""
    for url in DOWNLOAD_URLS:
        try:
            with urllib.request.urlopen(url, timeout=300) as response:
                payload = response.read()
        except Exception as exc:  # network/redirect/IO problems
            last_error = f"{url}: {type(exc).__name__}: {exc}"
            continue
        if len(payload) < 1_000_000:
            last_error = f"{url}: the download is only {len(payload)} bytes"
            continue
        if not payload.startswith(b"MZ"):
            last_error = f"{url}: the download is not a Windows executable"
            continue
        target.write_bytes(payload)
        return True, f"downloaded {url} ({len(payload)} bytes)"
    return False, last_error or "no download URL could be reached"


def install_with_official_installer(workdir: Path) -> tuple[bool, str]:
    """Run the official Inno Setup installer silently, then verify."""
    installer = workdir / "innosetup-setup.exe"
    ok, detail = download_inno_setup(installer)
    if not ok:
        return False, detail
    code, output = _run([
        str(installer), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
        "/SP-", "/CLOSEAPPLICATIONS",
    ])
    # The installer may report a non-zero status while still having
    # installed; the DETECT step that follows is the real verification.
    return True, f"ran the official installer (exit {code}) {output.strip()[:300]}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--github-env", metavar="FILE",
                        help="write ISCC_PATH into this GitHub Actions env file")
    parser.add_argument("--install", action="store_true",
                        help="install a compiler when none is usable")
    args = parser.parse_args(argv)

    if os.name != "nt":
        print("The Inno Setup compiler only exists on Windows; this step "
              "must run on a Windows runner.", file=sys.stderr)
        return 2

    compiler, detail = detect_iscc()
    if compiler is not None:
        print(f"  Inno Setup : {detail}")
        print(f"  compiler   : {compiler}")
    else:
        print(f"  Inno Setup : {detail}")
        if not args.install:
            print("  no usable compiler was found and --install was not given",
                  file=sys.stderr)
            return 1
        with tempfile.TemporaryDirectory(prefix="arena-innosetup-") as workdir:
            for installer in (install_with_chocolatey,):
                ok, message = installer()
                print(f"  install    : {message}")
                if not ok:
                    continue
                compiler, detail = detect_iscc()
                if compiler is not None:
                    print(f"  Inno Setup : {detail}")
                    break
            if compiler is None:
                ok, message = install_with_official_installer(Path(workdir))
                print(f"  install    : {message}")
                if not ok:
                    print(f"ERROR: {message}", file=sys.stderr)
                    return 1
                compiler, detail = detect_iscc()
                if compiler is None:
                    print(f"ERROR: Inno Setup was installed but is still not "
                          f"usable: {detail}", file=sys.stderr)
                    return 1
                print(f"  Inno Setup : {detail}")

    version = iscc_version(compiler)
    shown = ".".join(str(part) for part in version) if version else "unknown"
    print(f"  version    : {shown}")
    print(f"  compiler   : {compiler}")

    if args.github_env:
        with open(args.github_env, "a", encoding="utf-8") as handle:
            handle.write(f"ISCC_PATH={compiler}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
