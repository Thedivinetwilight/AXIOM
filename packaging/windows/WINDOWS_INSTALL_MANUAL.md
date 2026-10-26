# AXIOM Operator — Windows x64 Install Guide

**Version:** 3.7.0a3

**Applies to:** the AXIOM Operator loaderless installer and portable onedir folder built from the exact commit named in `BUILD_METADATA.json`.

This guide covers installation and Windows-specific operation. Read the bundled **AXIOM Operator User Manual** before connecting a demo or live account. It explains credentials, account-discovered instruments, risk, readiness, Arm/Activate, reviewed orders, reconciliation and recovery.

> **Trading risk:** AXIOM supports independent local paper, Vantage MT5 demo, and explicitly gated Vantage MT5 live modes. It does not claim profitability or live readiness. Package tests, demo fills and a successful connection do not remove market risk.

---

## 1. Requirements

- Windows 10 or 11, **x64**.
- A current Edge, Chrome or Firefox browser.
- No separate Python installation for the frozen executable.
- Per-user installation; administrator rights are not normally required.
- For Vantage execution modes or the optional MT5 paper-price feed: the Vantage MetaTrader 5 desktop terminal and an account/server explicitly supplied by Vantage. The official `MetaTrader5` Python package is bundled, but the desktop terminal is a separate prerequisite. The independent paper feed accepts DEMO credentials only.

AXIOM binds to loopback and opens a local browser page. Do not expose its API to a network or the internet.

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
LISTED_ASSETS.md
WINDOWS_APP_CONTROL.md
TERMINAL_PERFORMANCE.md
SHA256SUMS.txt
```

The release also includes test logs/manuals. Read `BUILD_METADATA.json` for the runner, commands, actual test outcomes, skipped demo/live checks and signing status. Application payloads use an explicit list of eight reviewed synthetic DEMO CSV/JSON fixtures; the build does not recursively bundle `data/`. It inspects the onefile archive, onedir tree and final ZIPs for credential/environment files, SQLite payloads, autonomy/cache/results state, local logs/locks and model weights. Per-user journals, credentials, evidence and settings remain outside the package.

Before running a download, verify its SHA-256 against `SHA256SUMS.txt`:

```powershell
Get-FileHash .\AXIOM-Operator-3.7.0a3-windows-x64-no-temp-install.zip -Algorithm SHA256
Get-FileHash .\AXIOM-Operator-3.7.0a3-windows-x64-portable.zip -Algorithm SHA256
Get-Content .\SHA256SUMS.txt
```

The file name and full 64-character hash must match the same release. Do not use a hash from another version.

**Signing:** these builds are **unsigned unless the exact release metadata explicitly says otherwise**. Windows SmartScreen/Smart App Control can show a warning or block an unknown publisher, and no verified publisher identity is claimed. Verify the source/release and hash; do not treat bypassing a warning as a safety check.

### 2.1 Error 4551 / Application Control

The 3.5.0a1 single installer was blocked on the reporting device while Inno Setup tried to execute its unsigned temporary `setup.tmp`. The workaround introduced in 3.5.0a2 is retained in 3.7.0a3: Inno `UseSetupLdr=no` for the installer and a PyInstaller onedir runtime for installation and portable use. This avoids the identified setup-loader and onefile runtime TEMP paths.

It is a packaging workaround, **not a policy bypass**. The packages are still unsigned. If an enforced Smart App Control or organization policy still blocks them, keep Windows security enabled and obtain trusted signing or administrator approval. Do not disable Application Control to install AXIOM. See `WINDOWS_APP_CONTROL.md`.

## 3. Install

1. Close running AXIOM instances.
2. Extract the **entire** `*-no-temp-install.zip` into a normal folder.
3. Confirm it contains one `*-no-temp-setup.exe` plus its matching `-0.bin` and `-1.bin` files. These sidecars are required; do not copy or run the EXE alone.
4. Run `AXIOM-Operator-<version>-windows-x64-no-temp-setup.exe` from that extracted folder.
5. Review the install location and shortcuts. For upgrade compatibility, the executable and default folder retain the technical name `AXIOM-Chart`; the displayed product is **AXIOM Operator**.
6. Launch from the Start Menu/desktop shortcut or the final wizard page.

The installer deploys `AXIOM-Chart.exe`, its `_internal` dependency folder, the operator UI, legacy chart, official MT5 Python dependency, manuals, version information and build metadata. It does not install the Vantage MT5 desktop terminal and does not contain broker credentials.

## 4. Portable onedir folder

1. Extract the **entire** `AXIOM-Operator-<version>-windows-x64-portable.zip`.
2. Open its `AXIOM-Operator` folder.
3. Run `AXIOM-Chart.exe` there, keeping `_internal` beside it. Do not copy only the EXE to another folder.

The portable folder contains the same application as the installed onedir runtime and avoids onefile `_MEI` TEMP extraction. “Portable” means no installer is required; it does **not** mean credential/data files travel with the folder. AXIOM writes operator data under the current user's `%LOCALAPPDATA%\AXIOM` directory.

The separate lone `AXIOM-Chart.exe` release asset is the legacy onefile build retained for compatibility. It can use TEMP extraction and is not the preferred App Control workaround.

Only run one writer against a data directory. The readiness writer-lock check must pass before Vantage activation.

## 5. First launch and local token

A console window opens and the browser navigates to:

```text
http://127.0.0.1:<port>/terminal
```

Keep the console open. Closing it stops the local application. The launcher prints sanitized paths, mode/feed information and the local URL; it deliberately does **not** print the API token or broker secrets.

On first run AXIOM creates per-user data similar to:

```text
%LOCALAPPDATA%\AXIOM\
  axiom.env             local process configuration and API_TOKEN
  axiom-paper.db        durable paper/Vantage journals and runtime state
  secrets\              Windows-DPAPI-protected Vantage credential files
  logs\                 sanitized application logs
```

When the operator page asks for the local token:

1. Open `%LOCALAPPDATA%\AXIOM\axiom.env` in Notepad.
2. Copy only the value after `API_TOKEN=` into the token field.
3. Do not share the file or value. It is remembered only for the current browser tab.

This token is separate from the Vantage login/password and from Alpaca/Upstox data credentials. It never authorizes live trading.

The initial mode is independent **LOCAL PAPER**, with Vantage disconnected/disarmed. If a Vantage environment was previously selected, startup still does not connect, Arm, Activate or submit an order.

## 6. Vantage MT5 setup

1. Install/open the Vantage MT5 terminal using Vantage's official instructions.
2. Confirm the exact server and account type yourself. For the requested live environment, the server text supplied for selection is `VantageMarkets-Live 13`; use it only if Vantage assigned it to your account.
3. In AXIOM, explicitly select **VANTAGE DEMO** or **VANTAGE LIVE**. A demo account is recommended for familiarization.
4. On **Connections**, enter the numeric login, trading password, exact server and terminal path if needed. Save or explicitly Replace the environment-specific profile.
5. Run **Test connection (read only)**. Verify the returned masked login/server and discovered instruments.

Secrets use Windows DPAPI for the current Windows user. They are not stored in `axiom.env` and are not returned by the API. The local token remains separate.

The **MT5 package available** indicator is only a module-capability check. It is not proof of terminal installation, login, connectivity, account permission, readiness or activation.

A read-only test does not place, cancel or modify orders. To enable reviewed manual orders, complete instrument selection, all risk limits, readiness, Arm and separate Activate, then review and confirm each order. See the main User Manual.

### 6.1 Fresh DEMO prices with LOCAL PAPER

This is separate from selecting the VANTAGE DEMO execution mode. Keep the global mode **LOCAL PAPER**, save the DEMO-only profile on **Connections**, and choose **Connect demo market data — read-only**. The isolated gateway cannot place, check, modify, cancel or close broker orders and cannot read broker positions/orders/history. Connecting never Arms, Activates or changes execution.

On **Charts**, select only an exact symbol returned by that demo account. Inspect its contract/CFD metadata and the fresh-quote label. **Start manual-only MT5 paper** requires fresh advancing ticks and validated closed bars; it creates no order and keeps automation off. Reviewed paper fills are local simulations in explicit units, not MT5 lots. Feed loss blocks new BUY entries, preserves paper state, and never falls back to replay.

Use the full **Local real-demo market-data verification procedure** in `USER_MANUAL.md`, including its isolated data-directory and account-currency/timeframe setup. CI does not perform this real-terminal check.

### 6.2 Redesigned charts, watchlists and ticket

The banner above all eight pages separates execution, market data and order authorization. Instrument stars and Watchlist only are local to the current browser tab and never select execution instruments; only **Save execution selection** changes the server set. Watchlists do not roam or survive closing the tab. Chart drawings are per-chart in-memory analysis annotations, not orders and not durable journal records.

Charts provide timeframe shortcuts, drag/wheel/keyboard zoom and pan, crosshair OHLC, EMA/SMA overlays and horizontal/trend drawings. These controls create no feed, selection or order request. The docked ticket distinguishes paper units from MT5 lots. Indicative value/spread are not final costs; MT5 margin and server risk come from read-only review, and broker deals remain authoritative.

For an external Vantage DEMO execution claim, the account owner must separately complete **Owner-run Vantage DEMO execution rehearsal** in `USER_MANUAL.md`. CI uses fake MT5 only and never logs into an external account. The LIVE-readiness checklist is also owner-only.

### 6.3 Performance and listed-product evidence

The installed `docs` folder includes `TERMINAL_PERFORMANCE.md`, `LISTED_ASSETS.md`, and the exact-commit `TERMINAL_BENCHMARK.json` / `TERMINAL_UI_BENCHMARK.json` generated on the Windows build runner. They report p50/p95/p99 for separate fake-provider and fake-canvas boundaries. They do not measure real Vantage/DEMO/LIVE network or broker latency, GPU presentation, profitability or live readiness. The Settings page can additionally measure the owner's actual local browser JavaScript/canvas duration.

True listed shares/ETFs require a separate provider and exact symbol + exchange/MIC + currency identity. This build has no listed execution adapter and prohibits MT5/CFD substitution. Existing Alpaca support remains read-only data for local paper fills. See `docs/LISTED_ASSETS.md` for setup and future provider acceptance gates.

## 7. Configuration and ports

Legacy process/feed settings remain in `%LOCALAPPDATA%\AXIOM\axiom.env`. The launcher still enforces the legacy engine's safe process configuration:

```dotenv
MODE=PAPER
LIVE_TRADING=false
BROKER=LOCAL_PAPER
```

These settings do **not** remove or disable the separately gated Vantage adapter in the Operator application. They prevent the legacy engine from being reconfigured as another broker.

The default local port is 8000; the launcher can select another port in its documented range and prints the actual URL. To request a specific starting port, set `AXIOM_PORT` before launch.

Vantage risk limits, selected instruments and credential metadata are managed in the authenticated Operator UI, not by adding broker passwords to `axiom.env`.

## 8. Upgrade

1. Download and hash-verify the new installer from the same trusted release process.
2. Close AXIOM and run the new installer over the existing installation.
3. Confirm the new version and `/terminal` after launch.

The installer replaces program files in the same per-user location and does not write the operator data directory. Configuration/local token, SQLite journals, paper state, protected credentials and logs are designed to remain in place.

The Windows acceptance test performs a real silent upgrade and checks preservation of the complete private configuration, a benign user-data sentinel and the SQLite database before launching the upgraded copy. Consult the exact release's `installer-test-log.txt` and `BUILD_METADATA.json`; do not infer this result from the script's presence.

An upgrade never means prior readiness/Arm/Activate survives. Startup is disconnected/disarmed and automation is off. Review migrations/release notes before upgrading a system with unresolved broker intents.

**Rollback:** uninstall without deleting user data, then install the older verified package. A rollback does not reconcile broker orders and cannot make an unknown outcome safe. Preserve the journal and reconcile in MT5/AXIOM before any new order.

## 9. Uninstall and data deletion

Use **Settings → Apps → Installed apps → AXIOM Operator → Uninstall** or the Start Menu uninstall entry.

Program files and shortcuts are removed. Interactive uninstall asks whether to delete `%LOCALAPPDATA%\AXIOM`:

- **No (default):** keep paper state, Vantage journals/intents, protected credentials, settings and logs for reinstall/recovery.
- **Yes:** permanently delete that operator data, including protected credential files and audit evidence. Verify broker state and make any needed lawful backups first.

Silent `/VERYSILENT` uninstall preserves user data. Keeping DPAPI files does not make them portable to another Windows user; treat them as sensitive even though they are encrypted.

Uninstalling AXIOM does not cancel MT5 orders, close positions or disable trading at Vantage. Check the broker terminal independently.

## 10. Windows verification commands

Maintainers can reproduce the build on a real Windows x64 machine with the pinned toolchain:

```powershell
cd packaging\windows
powershell -ExecutionPolicy Bypass -File .\build_windows.ps1 `
  -ExpectedCommit <full-source-commit> -Version 3.7.0a3
```

The script requires a clean source tree, runs focused and full Python tests, both pinned JavaScript harnesses, real Windows DPAPI tests with dummy secrets, PyInstaller onefile/onedir frozen-app checks, loaderless Inno packaging and ZIP structure checks. The release workflow separately extracts and tests both release ZIPs, including install/upgrade/uninstall and the portable no-`_MEI` runtime check. It refuses to produce a success result when a hard gate fails.

Run the separate installed-copy acceptance test against the generated setup file:

```powershell
powershell -ExecutionPolicy Bypass -File .\install_test.ps1 -DistDir dist
```

It checks install, launch, operator/chart/manual/static assets, authentication, safe Vantage defaults, MT5-module/DPAPI capability, upgrade preservation and uninstall. It uses no Vantage credential and performs no MT5 login or order.

A source checkout, Linux test, fake-MT5 test or script is not a substitute for these real-Windows results.

## 11. Troubleshooting

| Symptom | Action |
|---|---|
| SmartScreen / unknown publisher | Confirm the exact release and SHA-256. Builds are unsigned unless metadata says otherwise. |
| Executable flashes closed | Start it from PowerShell to read the sanitized error. Keep the console open during normal use. |
| Browser asks for token repeatedly | Token storage is tab-scoped. Read `API_TOKEN` from private `axiom.env`; it is not printed by the launcher. |
| Port 8000 belongs to another app | Close the old AXIOM instance or set `AXIOM_PORT`; use the actual URL printed by the launcher. |
| `database is locked` / writer lock fails | Close duplicate writers. Do not bypass the lock or copy a live database as a running backup. |
| MT5 package unavailable | Confirm you are using the current frozen Windows build and inspect its metadata. |
| MT5 terminal/login unavailable | Verify the separate Vantage terminal, executable path, numeric login/password and exact server. Never send credentials for support. |
| Demo feed stale/not advancing | Check terminal connection, exact symbol subscription and market hours. New paper BUY entries fail closed; replay is not substituted. |
| MT5 paper currency/timeframe refused | Use a separate new data directory configured for the demo account currency and intended timeframe; never delete a saved database to bypass the guard. |
| MT5 paper quantity seems different from MT5 | LOCAL PAPER uses explicit units, not MT5 lots; no contract-size or FX conversion is implied. |
| DPAPI credential cannot be read | Re-enter it as the same intended Windows user; DPAPI data is not a portable plaintext backup. |
| Vantage controls blocked | This is expected until connection, selection, risk, readiness, Arm, Activate and final review all pass. Read the specific blocker. |
| Upgrade preserved data but trading is disarmed | Required behavior. Reconnect/reconcile and repeat all approvals. |
| Uninstall did not flatten account | Uninstall is local only. Verify and manage broker orders/positions in MT5. |

Logs are under `%LOCALAPPDATA%\AXIOM\logs`. Inspect before sharing. Never attach `axiom.env`, DPAPI secrets, passwords, full account credentials or unredacted screenshots.

## 12. Non-claims and limits

- No Vantage demo/live login or order is performed by the installer tests.
- Installer/frozen tests do not connect a real demo feed; without a locally saved DEMO profile, market-data connect must fail closed without changing LOCAL PAPER.
- CI does not use real-money credentials and does not activate live trading.
- A demo account must be explicitly supplied for any separate demo E2E; otherwise it is skipped and reported.
- AXIOM does not discover instruments from a hard-coded Vantage product list; availability comes from the connected MT5 account.
- Vantage automation and TradingView webhook execution are not enabled in this version.
- Stop, Emergency stop, upgrade and uninstall do not prove broker flatness or cancellation.
- Tests and demo fills establish neither profitability nor live readiness.
