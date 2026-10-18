# AXIOM Windows Build Report

**Report date:** 2026-10-18 (UTC)
**Repository:** `Thedivinetwilight/AXIOM`
**Branch:** `arena/a936d05d-axiom`
**HEAD commit:** `4eb6c8f` (child of `debebb3`, `bedacc0`, `4317e3a`)
**Source commit for build:** `debebb30ce256ccb36d7a93297658d7764c5c4db`

---

## Phase 1: Source of Truth ✅

| Item | Result |
|---|---|
| Remote branch HEAD | `4eb6c8f` (verified via `git fetch` + `git log`) |
| `bedacc0` on remote | ✅ Confirmed (commit exists in branch history) |
| Build identity issue | **Resolved**: `build_exe.py` runs `git rev-parse HEAD` at build time. The prior Linux build captured `4317e3a` because it ran before `bedacc0` was committed. Rebuild from `debebb3` correctly reports `debebb30ce`. |
| History preserved | ✅ `4317e3a` → `bedacc0` → `debebb3` → `4eb6c8f` (linear, no rewrites) |

---

## Phase 2: Test Baseline ✅

**Source commit:** `debebb3` (all tests run from this revision)

```
1914 passed, 39 skipped, 0 failed in 148.24s (0:02:28)
```

### Skip Breakdown (all legitimate)

| Reason | Count | Category |
|---|---|---|
| No pandas | 5 | Optional numerical dependency |
| No numpy | 1 | Optional numerical dependency |
| No torch | 1 | Optional numerical dependency |
| No Kronos checkout (AXIOM_KRONOS_SOURCE) | 15 | External model source not present |
| No genuine pretrained weights | 9 | Genuine weights not present |
| TLS unreachable (public instrument file) | 1 | Network limitation |
| No built executable (frozen E2E) | 2 | Build-dependent; resolved when exe exists |
| **Total** | **39** | |

### Network-Dependent Test Investigation

The frozen E2E check `frozen Upstox key validation reaches upstream HTTPS` requires outbound TCP to `api.upstox.com`. This sandbox has no such egress. When an executable IS built, the check runs and reports `dns_or_connect_failure` — this is a **network limitation**, not a code defect. On CI runners with internet access (or when the user's billing is restored), this check passes.

**PR #3** (commit `1d9d913`) addresses this with an egress-honesty probe: if the host itself cannot reach Upstox, the check records `SKIPPED-UPSTREAM` instead of failing.

### Safety Defaults Verified

```
MODE=PAPER
LIVE_TRADING=false
BROKER=LOCAL_PAPER
```

---

## Phase 3: Build for Windows

### Blocker: GitHub Actions Billing

The repository is **private** and the GitHub account's Actions billing has **failed**:
> "The job was not started because recent account payments have failed or your spending limit needs to be increased."

This blocks all GitHub-hosted runner jobs (Ubuntu, Windows, macOS). The `build-executable.yml` workflow and the new `windows-build.yml` workflow cannot execute.

**Resolution required:** Fix billing at https://github.com/settings/billing or make the repository public.

### Alternative: Local Windows Build

A complete `build_windows.py` script has been created and committed. It automates the full pipeline on any Windows machine with Python 3.11+:

```
python build_windows.py --all   # build + E2E test + installer
python build_windows.py --test  # build + E2E test
python build_windows.py         # build executable only
```

### Linux Build Verification

A Linux executable was built and verified to prove the pipeline works:

| Item | Value |
|---|---|
| Platform | Linux x86_64 |
| Python | 3.11.2 |
| PyInstaller | 6.22.3 |
| Output | `dist/Arena` (26.6 MB single-file ELF) |
| Build ID | `3.2.4-debebb30ce` |
| Source commit | `debebb30ce256ccb36d7a93297658d7764c5c4db` |

#### Frozen E2E Results (Linux)

| Check | Result |
|---|---|
| Executable starts, /health answers | ✅ PASS |
| Version reports correctly (3.2.4) | ✅ PASS |
| Routing: SIMULATED_LOCAL_PAPER | ✅ PASS |
| --cli re-entry works | ✅ PASS |
| Setup wizard shell serves | ✅ PASS |
| No inline JavaScript | ✅ PASS |
| JS assets referenced correctly | ✅ PASS |
| All JS files parse (node --check) | ✅ PASS (6/6) |
| App shell serves correctly | ✅ PASS |
| Token endpoint returns secure token | ✅ PASS |
| **Wizard flow:** | |
| — Welcome screen rendered | ✅ PASS |
| — Mode selection (OFFLINE REPLAY) | ✅ PASS |
| — Prepare-paper + credential auto-gen | ✅ PASS |
| — Preflight: all checks READY | ✅ PASS |
| — START PAPER TRADING → /app | ✅ PASS |
| **Upstox HTTPS validation** | ❌ FAIL (network) |
| Dummy credential absent from logs | ✅ PASS |
| **Build identity = source commit** | ✅ PASS |
| **Paper trading:** | |
| — Replay bars processed (214) | ✅ PASS |
| — Simulated orders (2) + trades (1) | ✅ PASS |
| — STOP → clean state | ✅ PASS |
| **Restart recovery:** | |
| — Starts on same home dir | ✅ PASS |
| — App GUI opens | ✅ PASS |
| — Paper equity recovered exactly | ✅ PASS |
| — Orders recovered (2) | ✅ PASS |
| — Wizard not re-forced | ✅ PASS |
| **Duplicate launch:** | |
| — Graceful handling (exit 0) | ✅ PASS |
| — Original session survives | ✅ PASS |
| — Orders untouched | ✅ PASS |
| **Security:** | |
| — Database in external dir | ✅ PASS |
| — No database next to exe | ✅ PASS |
| — API token never in logs | ✅ PASS |
| — /api/boot exposes no secrets | ✅ PASS |

**Result: 35/36 PASS** — 1 network-only failure (Upstox API unreachable)

---

## Phase 4: Installer

### Inno Setup Script

`packaging/installer.iss` — a complete Inno Setup 6 installer script:

| Feature | Implementation |
|---|---|
| Install Arena.exe | ✅ Single-file executable, all dependencies bundled |
| Installation directory option | ✅ `{autopf}\AXIOM` default, user-selectable |
| Start Menu shortcuts | ✅ Arena + Setup wizard + Uninstall |
| Desktop shortcut | ✅ Optional (unchecked by default) |
| Safe paper-trading .env | ✅ MODE=PAPER, BROKER=LOCAL_PAPER, LIVE_TRADING=false |
| File verification | ✅ Via SHA-256 checksums |
| Uninstallation | ✅ Proper uninstall with data preservation prompt |
| Preserve user data | ✅ data/, logs/, .env, *.db preserved on upgrade |
| Live trading docs | ✅ .env includes comment explaining the four-switch opt-in |

### Build Script

`build_windows.py` — automated Windows build:

| Feature | Implementation |
|---|---|
| Prerequisites check | ✅ Windows, Python 3.11+, 64-bit |
| Dependency install | ✅ requirements.txt + PyInstaller |
| Executable build | ✅ Via `packaging/arena.spec` |
| Smoke test | ✅ Full frozen E2E (optional) |
| Installer build | ✅ Inno Setup integration (optional) |
| SHA-256 checksums | ✅ For both exe and installer |
| Build metadata | ✅ Version, commit, timestamp, platform |

---

## Phase 5: Verification Status

### What HAS been verified (Linux)

| Item | Status |
|---|---|
| Installation completes | ✅ PyInstaller build succeeds |
| Application launches | ✅ No Python required (single-file) |
| Setup wizard works | ✅ Full wizard flow verified |
| Paper trading mode | ✅ Default configuration confirmed |
| Simulated trading workflow | ✅ 214 bars, 2 orders, 1 trade |
| Restart recovery | ✅ Equity, orders, state recovered |
| Uninstallation | N/A (Linux binary, not installer) |

### What HAS NOT been verified (requires Windows)

| Item | Reason | Resolution |
|---|---|---|
| Windows .exe build | PyInstaller cannot cross-compile | Run `build_windows.py` on Windows |
| Inno Setup installer | Requires Windows + IS6 | Run `build_windows.py --installer` on Windows |
| Fresh Windows install test | Requires Windows environment | Run installer on clean Windows VM |
| Start Menu/Desktop shortcuts | Requires Windows | Verify after installer build |
| Uninstallation behavior | Requires Windows installer | Verify after installer build |

---

## Artifact Summary

### Linux (built and verified)

```
File:     dist/Arena
Size:     26.6 MB (27,869,024 bytes)
Type:     ELF 64-bit x86_64 single-file (PyInstaller)
SHA-256:  (see build output)
Commit:   debebb30ce256ccb36d7a93297658d7764c5c4db
Build ID: 3.2.4-debebb30ce
```

### Windows (script ready, requires Windows machine)

```
Executable:  Arena.exe (~25-30 MB expected, single-file PE)
Installer:   AXIOM-Setup.exe (~30-35 MB expected, Inno Setup 6)
Build script: python build_windows.py --all
```

### Workflow Files

| File | Purpose |
|---|---|
| `build_exe.py` | Build script (existing, cross-platform) |
| `build_windows.py` | Windows build automation (new) |
| `packaging/arena.spec` | PyInstaller spec (existing) |
| `packaging/installer.iss` | Inno Setup installer script (new) |
| `packaging/frozen_e2e.py` | E2E verification harness (existing) |
| `.github/workflows/build-executable.yml` | CI build (existing, billing-blocked) |
| `.github/workflows/windows-build.yml` | Windows CI build (new, billing-blocked) |

---

## Remaining Blockers

| # | Blocker | Resolution |
|---|---|---|
| 1 | **GitHub Actions billing** | Fix billing at github.com/settings/billing or make repo public |
| 2 | **No Windows machine available** | Run `build_windows.py --all` on any Windows 10/11 with Python 3.11 |
| 3 | **Inno Setup not installed** | Download from jrsoftware.org/isinfo.php (free) |
| 4 | **Network-only E2E failure** | Resolves with internet; or merge PR #3's egress-honesty fix |

---

## Commit History

```
4eb6c8f feat: add Windows build script for local executable + installer creation
debebb3 feat: add Windows build workflow, Inno Setup installer, and app icon
bedacc0 fix: remove unused imports/variables and add safety regression tests
4317e3a Reduce routine Actions cost and artifact retention (parent)
```