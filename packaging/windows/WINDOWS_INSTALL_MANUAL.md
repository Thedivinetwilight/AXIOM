# AXIOM Operator — Windows x64 Install Guide

**Version:** 3.7.0a9

**Applies to:** the AXIOM Operator loaderless installer and portable onedir folder built from the exact commit named in `BUILD_METADATA.json`.

This guide covers Windows installation, verification, upgrade, and uninstallation. Read the bundled **AXIOM Operator User Manual** (`USER_MANUAL.md`) and **Owner Setup Guide** (`docs/OWNER_SETUP_GUIDE.md`) before connecting Upstox (NSE `INR ₹`) or Alpaca (NASDAQ `USD $`) credentials.

> **Trading risk:** AXIOM supports independent local paper, live-market NSE (`INR` via Upstox) and NASDAQ (`USD` via Alpaca) SQLite paper simulation, and separately gated broker execution (OFF by default). Package tests, simulated fills, and read-only connection checks do not establish profitability or remove market risk.

---

## 1. Requirements

- Windows 10 or 11, **x64**.
- A current Edge, Chrome, or Firefox browser.
- No separate Python installation is required to run the frozen `AXIOM-Chart.exe` operator console.
- Optional for **`10 AI & Model Lab`** Kronos CPU inference: CPython `3.11` or `3.12` **x64** with `pip` (`py -3.11-64` or `py -3.12-64`).
- Per-user installation; administrator rights are not normally required.

AXIOM binds strictly to loopback (`127.0.0.1`) and opens `/terminal` locally. Do not expose its API to a network or the internet.

---

## 2. Release files and verification

A complete release provides, for the same source commit:

```text
AXIOM-Operator-<version>-windows-x64-no-temp-install.zip  # recommended installer workaround
AXIOM-Operator-<version>-windows-x64-portable.zip         # recommended no-installer workaround
AXIOM-Chart.exe                                           # legacy onefile compatibility build
BUILD_METADATA.json
installer-test-log.txt
portable-test-log.txt
TERMINAL_BENCHMARK.json         # exact-commit fake-provider Windows measurements
TERMINAL_UI_BENCHMARK.json      # exact-commit fake-canvas measurements
kronos-status.json              # installed-app pinned setup + real CPU inference evidence
kronos-small.json               # authoritative artifact pins
kronos-runtime-requirements.txt # exact optional CPU runtime lock
LISTED_ASSETS.md
WINDOWS_APP_CONTROL.md
TERMINAL_PERFORMANCE.md
SHA256SUMS.txt
```

Before running a download, verify its SHA-256 against `SHA256SUMS.txt`:

```powershell
Get-FileHash .\AXIOM-Operator-3.7.0a9-windows-x64-no-temp-install.zip -Algorithm SHA256
Get-FileHash .\AXIOM-Operator-3.7.0a9-windows-x64-portable.zip -Algorithm SHA256
Get-Content .\SHA256SUMS.txt
```

The file name and full 64-character hash must match the same release. **Signing:** builds are **unsigned unless the exact release metadata explicitly says otherwise**.

### 2.1 Error 4551 / Application Control

The workaround introduced in 3.5.0a2 is retained in 3.7.0a9: Inno `UseSetupLdr=no` for the installer and a PyInstaller onedir runtime for installation and portable use. This avoids Inno Setup's temporary `setup.tmp` and PyInstaller onefile `_MEI` TEMP extraction paths.

It is a packaging workaround, **not a policy bypass**. The packages remain unsigned. Keep Windows security and Application Control enabled; if an enforced Smart App Control or WDAC policy blocks unsigned binaries, trusted signing or administrator approval is required. See `WINDOWS_APP_CONTROL.md`.

---

## 3. Install

1. Close running AXIOM instances.
2. Extract the **entire** `*-no-temp-install.zip` into a normal folder.
3. Confirm it contains `*-no-temp-setup.exe` plus its matching `-0.bin` and `-1.bin` sidecar files in the same folder. Do not copy or run the EXE alone.
4. Run `AXIOM-Operator-<version>-windows-x64-no-temp-setup.exe` from that extracted folder.
5. For upgrade compatibility, the executable and default install folder retain the technical name `AXIOM-Chart` (`%LOCALAPPDATA%\Programs\AXIOM-Chart`); the displayed product is **AXIOM Operator**.
6. Launch from the Start Menu/desktop shortcut or the final wizard page.

---

## 4. Portable onedir folder

1. Extract the **entire** `AXIOM-Operator-<version>-windows-x64-portable.zip`.
2. Open its `AXIOM-Operator` folder.
3. Run `AXIOM-Chart.exe` there, keeping `_internal` beside it. Do not copy only the EXE out of that folder.

The portable runtime avoids onefile `_MEI` TEMP extraction and stores writable operator data under `%LOCALAPPDATA%\AXIOM`. Do not run the portable copy and installed copy concurrently against the same data directory.

---

## 5. First launch and local token

A console window opens and launches your browser at:

```text
http://127.0.0.1:<port>/terminal
```

Keep the console window open while using AXIOM. On first run AXIOM creates per-user data under:

```text
%LOCALAPPDATA%\AXIOM\
  axiom.env             local process configuration and API_TOKEN
  axiom-paper.db        durable local paper SQLite state
  autonomy\             NSE/NASDAQ catalogs, paper ledgers, Kronos artifacts, DPAPI credentials
  logs\                 sanitized application logs
```

When `/terminal` prompts for the local `API_TOKEN`:

1. Open `%LOCALAPPDATA%\AXIOM\axiom.env` in Notepad.
2. Copy only the value after `API_TOKEN=` into the unlock dialog.
3. Never share `axiom.env` or `API_TOKEN`. It is remembered only for the current browser tab and is separate from Upstox and Alpaca credentials.

---

## 6. Upstox (NSE `INR`) & Alpaca (NASDAQ `USD`) configuration

All provider credentials are configured inside the authenticated **`07 Connections`** workspace and encrypted with **Windows DPAPI for the current Windows user** (never stored in plaintext in `axiom.env` and never echoed by the API):

1. **NSE Read-Only Market Data (`Upstox`)**: Paste your daily read-only `UPSTOX_ACCESS_TOKEN` and click **Verify NSE quote (RELIANCE)** (`0` order calls). Use **Refresh NSE BOD master** on `02 Watchlists & search` to sync NSE listings.
2. **NASDAQ Read-Only Market Data (`Alpaca`)**: Enter your `ALPACA_MARKET_DATA` Key ID & Secret (`iex` free real-time feed or `sip` paid consolidated tape) and click **Verify NASDAQ quote (AAPL)** (`0` order calls). Use **Sync NASDAQ universe** on `02 Watchlists & search` to sync active NASDAQ equities and ETFs.
3. **Separate Broker Execution Slots (OFF by default)**: `ALPACA_BROKER_PAPER`, `ALPACA_BROKER_LIVE`, and `UPSTOX_LIVE_ACCESS_TOKEN` live in separate DPAPI slots. Saving or testing credentials never Arms or Activates trading; see `USER_MANUAL.md` and `docs/OWNER_SETUP_GUIDE.md` for full readiness, Arm, and Activate steps.

---

## 7. Configuration, upgrade, and uninstall

### 7.1 Legacy process safety switches

`%LOCALAPPDATA%\AXIOM\axiom.env` enforces paper-only startup for the legacy engine:

```dotenv
MODE=PAPER
LIVE_TRADING=false
BROKER=LOCAL_PAPER
```

To request a different starting loopback port than `8000`, set `AXIOM_PORT` before launch.

### 7.2 Upgrade and rollback

1. Close AXIOM, verify the new release's SHA-256 against `SHA256SUMS.txt`, and run the new installer over the existing per-user installation.
2. `%LOCALAPPDATA%\AXIOM` (`axiom.env`, SQLite databases, DPAPI credentials, Kronos runtime/weights, and logs) is preserved across upgrades and silent uninstalls.
3. Startup after upgrade is always disarmed with live execution OFF. Prior readiness, Arm, or Activate state never carries over.
4. Interactive uninstall via **Settings → Apps → Installed apps → AXIOM Operator → Uninstall** prompts whether to keep (**No**, default) or permanently delete (**Yes**) `%LOCALAPPDATA%\AXIOM`. Uninstalling AXIOM is local only and never cancels open orders or flattens positions at Upstox or Alpaca.

---

## 8. Windows build & acceptance commands

Maintainers can reproduce the full Windows x64 build and smoke gates from a clean source tree:

```powershell
cd packaging\windows
powershell -ExecutionPolicy Bypass -File .\build_windows.ps1 `
  -ExpectedCommit <full-source-commit> -Version 3.7.0a9
powershell -ExecutionPolicy Bypass -File .\install_test.ps1 -DistDir dist
```

---

## 9. Troubleshooting

| Symptom | Action |
|---|---|
| SmartScreen / unknown publisher | Confirm the exact release and SHA-256 in `SHA256SUMS.txt`. Builds are unsigned unless metadata states otherwise. |
| Browser asks for token | Read `API_TOKEN` from `%LOCALAPPDATA%\AXIOM\axiom.env`; storage is tab-scoped (`sessionStorage`). |
| Port 8000 busy | Close duplicate AXIOM instances or set `AXIOM_PORT`; check the console window for the bound port. |
| `database is locked` / lease busy | Close duplicate AXIOM processes sharing `%LOCALAPPDATA%\AXIOM`. |
| Upstox token `EXPIRED` | Upstox daily tokens expire around `03:30 IST`; paste a fresh read-only token on `07 Connections`. |
| Alpaca `SIP` HTTP `403` | Switch the Alpaca Market Data feed selector on `07 Connections` to `iex` unless subscribed to paid SIP. |
| DPAPI secret unreadable | Re-enter credentials under the same Windows user account; DPAPI blobs are user-bound. |
