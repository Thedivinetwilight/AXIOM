# Windows Application Control / Error 4551 workaround

## Observed problem

AXIOM Operator 3.5.0a1 was reported on 2026-10-25 to abort before installation
with:

> Unable to execute file in the temporary directory. Setup aborted. Error 4551:
> An Application Control policy has blocked this file.

The Windows Security notification named the Inno Setup temporary `setup.tmp`
component and said its publisher could not be confirmed. This is a Windows
execution-policy failure before AXIOM starts, not a Vantage connection, order,
paper fill, DEMO verification or LIVE verification result.

Microsoft documents that Smart App Control can block unknown unsigned code.
Inno Setup documents that its default SetupLdr copies the actual setup program
to a subdirectory of the user's TEMP directory and runs it there. The 3.5.0a1
release was unsigned and used that default single-EXE loader.

## 3.5.0a2 workaround

The workaround changes packaging, not Windows security and not trading safety:

1. **Loaderless installation:** `UseSetupLdr=no` compiles a setup executable
   plus required `-0.bin` and `-1.bin` sidecars. Inno Setup documents that this
   mode does not copy the setup program to TEMP. The download is therefore a
   ZIP, not a lone setup EXE.
2. **Onedir installed runtime:** the installer deploys `AXIOM-Chart.exe` and
   its `_internal` dependencies into the selected per-user installation folder.
   It does not use the PyInstaller onefile `_MEI` temporary extraction path.
3. **Onedir portable ZIP:** the same folder-based runtime is available without
   installation. Extract the entire ZIP and keep the executable beside
   `_internal`.
4. The legacy onefile `AXIOM-Chart.exe` remains for upgrade compatibility and
   for devices where it already works. It is not the preferred App Control
   workaround because onefile builds can use TEMP extraction.

These changes do not disable Smart App Control, WDAC, SmartScreen, Defender,
authentication, DPAPI, risk bounds, readiness, Arm/Activate, review,
idempotency, emergency stop or reconciliation. No policy exclusion or security
registry change is made.

## Use the installer workaround

1. Download `AXIOM-Operator-<version>-windows-x64-no-temp-install.zip` and
   `SHA256SUMS.txt` from the same exact release.
2. Verify the ZIP's SHA-256 against the manifest. A hash proves integrity, not
   publisher trust.
3. Extract the **entire** ZIP to a normal folder such as Downloads.
4. Confirm the folder contains one `*-no-temp-setup.exe`, its matching
   `-0.bin` and `-1.bin`, `README-FIRST.txt`, manuals and source record.
5. Run the EXE. Do not rename it independently of its sidecars, delete the BIN
   files, or copy the EXE somewhere by itself.
6. Installation remains per-user. Existing operator data under
   `%LOCALAPPDATA%\AXIOM` is preserved by upgrade and silent uninstall.

Inno Setup can use temporary self-copies during uninstall; `UseSetupLdr=no`
specifically addresses the setup-loader path implicated by Error 4551. It is
not a promise that every executable or uninstall operation will be accepted by
every Application Control policy.

## Use the portable workaround

1. Download and hash-verify
   `AXIOM-Operator-<version>-windows-x64-portable.zip`.
2. Extract the entire ZIP.
3. Open the extracted `AXIOM-Operator` folder and run `AXIOM-Chart.exe` there.
4. Keep `_internal` and the bundled resources beside it. Do not copy only the
   EXE to Desktop or another folder.

The portable folder is independent of the legacy single-file executable. Both
use the same per-user operator data directory, so run only one writer against a
data directory at a time.

## Remaining trust boundary

All 3.5.0a2 packages remain **UNSIGNED**. Loaderless/onedir packaging avoids
the two identified temporary-execution paths; it does not make unsigned code
trusted and does not guarantee acceptance by an enforced Smart App Control or
organization-managed WDAC policy. If Windows still blocks the main executable
or a dependency, stop and keep the policy enabled. The durable public-trust
solution is appropriate code signing or explicit approval by the device
administrator. Do not request or install a private signing key through chat.

The Windows CI runner verifies the exact commit, full tests, ZIP structure,
loaderless install/upgrade/uninstall, installed onedir runtime, extracted
portable startup, authenticated safe defaults and absence of a new `_MEI`
TEMP directory during the portable run. The hosted runner does not reproduce
the owner's enforced Smart App Control policy, so on-device acceptance remains
separate evidence. No real DEMO or LIVE account is used by these package tests.
