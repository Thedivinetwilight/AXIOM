; AXIOM Arena - Inno Setup Installer Script
; Builds a Windows installer for the Arena paper-trading platform.
;
; Usage (from CI or local):
;   ISCC.exe installer.iss /DArenaExe="path\to\Arena.exe" /DAppVersion="3.2.4" /O"..\release" /F"AXIOM-Setup"
;
; The installer:
;   - Installs Arena.exe (single-file, zero-dependency runtime)
;   - Creates Start Menu shortcuts
;   - Optional desktop shortcut
;   - Initializes a safe paper-trading .env (MODE=PAPER, BROKER=LOCAL_PAPER)
;   - Preserves user data on upgrade (data/, *.db, logs/, .env)
;   - Provides proper uninstall with data preservation option

#ifndef ArenaExe
  #error "ArenaExe must be defined: ISCC.exe installer.iss /DArenaExe=path\to\Arena.exe"
#endif

#ifndef AppVersion
  #define AppVersion "3.2.4"
#endif

#define MyAppName "AXIOM Arena"
#define MyAppShortName "AXIOM"
#define MyAppPublisher "Thedivinetwilight"
#define MyAppURL "https://github.com/Thedivinetwilight/AXIOM"
#define MyAppExeName "Arena.exe"

[Setup]
AppId={{B8E5C7A2-4F31-4D29-9E8A-1A2B3C4D5E6F}
AppName={#MyAppName}
AppVersion={#AppVersion}
AppVerName={#MyAppName} {#AppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}/releases
DefaultDirName={autopf}\{#MyAppShortName}
DefaultGroupName={#MyAppName}
; No LicenseFile — repository has no LICENSE file
OutputBaseFilename=AXIOM-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
SetupIconFile=..\app\static\arena.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName} {#AppVersion}
VersionInfoVersion={#AppVersion}.0
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription=AXIOM Arena - Quantitative Research & Paper Trading Platform
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#AppVersion}
; Don't close applications during install (Arena manages its own lifecycle)
CloseApplications=no
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; The Arena executable (single-file PyInstaller build, includes all dependencies)
Source: "{#ArenaExe}"; DestDir: "{app}"; Flags: ignoreversion

[Dirs]
; Create user data directories (preserved on upgrade/uninstall when user chooses)
Name: "{app}\data"; Flags: uninsneveruninstall
Name: "{app}\logs"; Flags: uninsneveruninstall

[Icons]
; Start Menu shortcuts
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Comment: "Start AXIOM Arena paper trading"
Name: "{group}\{#MyAppName} (Setup)"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--setup"; Comment: "Open AXIOM Arena setup wizard"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
; Desktop shortcut (optional task)
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon; Comment: "Start AXIOM Arena paper trading"

[Run]
; Initialize safe paper-trading configuration on first install
Filename: "{app}\{#MyAppExeName}"; Parameters: "--setup"; Description: "Run the setup wizard"; Flags: postinstall skipifsilent runmaximized nowait

[INI]
; Write a safe default .env on fresh install (won't overwrite existing)
Filename: "{app}\.env"; Section: "defaults"; Key: "MODE"; String: "PAPER"; Flags: uninsneveruninstall createkeyifdoesntexist
Filename: "{app}\.env"; Section: "defaults"; Key: "BROKER"; String: "LOCAL_PAPER"; Flags: uninsneveruninstall createkeyifdoesntexist
Filename: "{app}\.env"; Section: "defaults"; Key: "LIVE_TRADING"; String: "false"; Flags: uninsneveruninstall createkeyifdoesntexist

[Code]
// ---- Custom wizard pages for first-run configuration ----

var
  DataDirPage: TInputDirWizardPage;
  CreateShortcutsCheckbox: TNewCheckBox;
  PreserveDataCheckbox: TNewCheckBox;

procedure InitializeWizard;
begin
  // Custom page: choose data directory
  DataDirPage := CreateInputDirPage(wpSelectDir,
    'Data Directory', 'Where should AXIOM store its data?',
    'AXIOM stores market data, databases, and logs in this folder.' + #13#10 +
    'This folder will be preserved when you upgrade or uninstall.' + #13#10#13#10 +
    'Select a data directory:',
    False, '');
  DataDirPage.Add('');
  DataDirPage.Values[0] := ExpandConstant('{localappdata}\AXIOM\data');
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  // Skip data dir page on silent install
  Result := WizardIsSilent and (PageID = DataDirPage.ID);
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  EnvFile: string;
  DataDir: string;
begin
  if CurStep = ssPostInstall then
  begin
    // Write safe paper-trading .env if it doesn't already exist
    EnvFile := ExpandConstant('{app}\.env');
    if not FileExists(EnvFile) then
    begin
      DataDir := DataDirPage.Values[0];
      // Create the data directory
      ForceDirectories(DataDir);

      // Write a safe default configuration
      SaveStringToFile(EnvFile,
        '# AXIOM Arena - Safe Paper Trading Configuration' + #13#10 +
        '# Generated by installer on ' + GetDateTimeString('yyyy-mm-dd hh:nn:ss', #0, #0) + #13#10 +
        '# This is a PAPER trading configuration. No real money is involved.' + #13#10 +
        '' + #13#10 +
        'MODE=PAPER' + #13#10 +
        'BROKER=LOCAL_PAPER' + #13#10 +
        'LIVE_TRADING=false' + #13#10 +
        'INITIAL_CAPITAL=100000' + #13#10 +
        'CURRENCY=INR' + #13#10 +
        'DEFAULT_SYMBOL=DEMO' + #13#10 +
        'DEFAULT_TIMEFRAME=1D' + #13#10 +
        'DATA_SOURCE=replay' + #13#10 +
        'LOG_LEVEL=INFO' + #13#10 +
        'DATA_DIR=' + DataDir + #13#10 +
        '' + #13#10 +
        '# Live trading is disabled. To enable it, you must:' + #13#10 +
        '# 1. Set MODE=LIVE' + #13#10 +
        '# 2. Set LIVE_TRADING=true' + #13#10 +
        '# 3. Set BROKER=UPSTOX_LIVE' + #13#10 +
        '# 4. Set LIVE_CONFIRM=I-ACCEPT-REAL-MONEY-RISK' + #13#10 +
        '# 5. Set non-zero MAX_POSITION_VALUE, MAX_OPEN_POSITIONS, MAX_DAILY_LOSS, MAX_TRADES_PER_DAY' + #13#10 +
        '# See LIVE_TRADING_ENABLEMENT.md for details.' + #13#10,
        False);
    end;
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): string;
begin
  // Check if Arena is currently running
  Result := '';
  // The executable manages its own instance guard, so no manual check needed
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  KeepData: Integer;
begin
  if CurUninstallStep = usUninstall then
  begin
    // Ask user if they want to keep their data
    KeepData := MsgBox(
      'Do you want to keep your trading data, databases, and logs?' + #13#10#13#10 +
      'Click Yes to preserve your data (recommended).' + #13#10 +
      'Click No to delete everything including your data.',
      mbConfirmation, MB_YESNO or MB_DEFBUTTON1);
    if KeepData = IDNO then
    begin
      // User chose to delete data
      DelTree(ExpandConstant('{app}\data'), True, True, True);
      DelTree(ExpandConstant('{app}\logs'), True, True, True);
      DeleteFile(ExpandConstant('{app}\.env'));
      DeleteFile(ExpandConstant('{app}\trading_bot.db'));
      DeleteFile(ExpandConstant('{app}\trading_bot.db-shm'));
      DeleteFile(ExpandConstant('{app}\trading_bot.db-wal'));
    end;
  end;
end;

[UninstallDelete]
; Only delete the application files, not user data (unless user chose to)
Type: filesandordirs; Name: "{app}\__pycache__"
Type: filesandordirs; Name: "{app}\build"
Type: filesandordirs; Name: "{app}\dist"
Type: files; Name: "{app}\*.pyc"