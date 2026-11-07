# Windows acceptance — **NOT RUN** (no Windows host available)

Status: **NOT RUN**. This file records what was attempted, what the environment
actually was, and the exact commands that stay unexecuted. It is **not** a
Windows result, and no Linux run is offered as a substitute.

Recorded: 2026-11-07 (sandbox clock), branch `arena/8d3d005e-axiom`.

## 1. What was probed, and what came back

| Probe | Command | Result |
| --- | --- | --- |
| Host kernel | `uname -a` | `Linux e2b.local 6.1.158+ #1 SMP PREEMPT_DYNAMIC ... x86_64 GNU/Linux` |
| Windows compatibility layer | `command -v wine` / `wine64` | **ABSENT** |
| Windows shell | `command -v powershell` / `pwsh` | **ABSENT** |
| Windows installer compiler | `command -v makensis` | **ABSENT** |

Because the host is Linux and none of `wine`, `pwsh`/`powershell` or `makensis`
exists, the packaged Windows build cannot be produced or launched here, and the
PowerShell acceptance scripts cannot execute. Running the Python test suite on
Linux is **not** Windows acceptance and is not counted as such.

## 2. Windows-only artifacts that exist and are covered by Linux static gates only

| Artifact | Static coverage on Linux | Native execution |
| --- | --- | --- |
| `packaging/windows/build_windows.ps1` | `tests/test_windows_app_control_packaging.py` (text/structure assertions) | **not run** |
| `packaging/windows/install_test.ps1` | same static assertions | **not run** |
| `packaging/windows/axiom.spec` | spec-input assertions; PyInstaller itself cannot run here (no `libpython3.11.so.1.0`) | **not run** |
| `.github/workflows/windows-release.yml` | workflow text is asserted | **not run** in this session |
| `docs/WINDOWS_APP_CONTROL.md`, `packaging/windows/WINDOWS_INSTALL_MANUAL.md` | read/asserted | n/a |

## 3. Commands that remain unexecuted (for an operator on Windows 10/11 x64)

```powershell
# 1. Build (from the repository root, Windows, x64 CPython)
pwsh -File packaging\windows\build_windows.ps1

# 2. Installer acceptance against the installed copy, not the build directory
pwsh -File packaging\windows\install_test.ps1 -DistDir dist

# 3. Packaged app answers loopback /health, then /terminal
.\dist\AXIOM\AXIOM.exe            # expect /health 200 and /terminal 200

# 4. Packaged `live-readiness` CLI (the gap this item tracks)
.\dist\AXIOM\AXIOM.exe live-readiness
.\dist\AXIOM\AXIOM.exe live-readiness --json
echo $LASTEXITCODE                 # record the exit code for both invocations
```

For step 4, record for each invocation: the exit code, whether the report prints
`coverage.windows_acceptance`, `live_verified`, and whether any credential value
appears in stdout/stderr (it must not). A non-zero exit code is an acceptable,
informative result — record it, do not tune it away.

## 4. What this means for readiness

`docs/LIVE_EXECUTION_READINESS.md` §5 and the readiness report's `coverage`
block both state `windows_acceptance: "NOT_RUN"`. That label stays until a real
Windows run produces the results above. No claim in this repository depends on a
Windows run having happened.
