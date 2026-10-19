; Inno Setup script for the AXIOM Arena Windows install.
;
; Build it with:   python packaging\build_installer.py --verify
; (that script drives ISCC.exe and verifies the produced package)
;
; What the installer does, and what it deliberately does NOT do:
;
;   * it installs the single-file Arena.exe, the recovery documentation and
;     a SAFE FALLBACK configuration beside the executable;
;   * it NEVER touches %LOCALAPPDATA%\Arena, so an existing paper account,
;     its orders, its fills and its research data survive an upgrade, a
;     reinstall and an uninstall;
;   * it never creates, resets, migrates or deletes a database;
;   * it registers an Add/Remove Programs entry and an optional desktop
;     shortcut, and never needs elevation.
;
; The fallback .env is only a fallback: the runtime precedence is the
; process environment, then %LOCALAPPDATA%\Arena\.env (the per-user home the
; setup wizard writes), then this executable-adjacent file, then the bundled
; root .env.

; The build script passes the three absolute paths below, so this script
; never has to guess which directory a relative path belongs to.
#ifndef ArenaExe
  #error "ArenaExe must be defined"
#endif
#ifndef RecoveryDoc
  #error "RecoveryDoc must be defined"
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
OutputBaseFilename=AXIOM-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName} {#AppVersion}
VersionInfoVersion={#AppVersion}.0
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription=AXIOM Arena - Quantitative Research & Paper Trading Platform
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#AppVersion}
CloseApplications=no
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#ArenaExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#RecoveryDoc}"; DestDir: "{app}"; Flags: ignoreversion isreadme

[Dirs]
Name: "{app}\data"; Flags: uninsneveruninstall
Name: "{app}\logs"; Flags: uninsneveruninstall

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Comment: "Start AXIOM Arena paper trading"
Name: "{group}\{#MyAppName} (Setup)"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--setup"; Comment: "Open setup wizard"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon; Comment: "Start AXIOM Arena paper trading"

[Run]
Filename: "{app}\{#MyAppExeName}"; Parameters: "--setup"; Description: "Run setup wizard"; Flags: postinstall skipifsilent nowait

; These defaults sit beside the executable and are a fallback only. The
; setup wizard writes the user's settings to the per-user Arena home.
[INI]
Filename: "{app}\.env"; Section: "setup"; Key: "MODE"; String: "PAPER"
Filename: "{app}\.env"; Section: "setup"; Key: "BROKER"; String: "LOCAL_PAPER"
Filename: "{app}\.env"; Section: "setup"; Key: "LIVE_TRADING"; String: "false"
Filename: "{app}\.env"; Section: "setup"; Key: "INITIAL_CAPITAL"; String: "100000"
Filename: "{app}\.env"; Section: "setup"; Key: "CURRENCY"; String: "INR"
Filename: "{app}\.env"; Section: "setup"; Key: "DEFAULT_SYMBOL"; String: "DEMO"
Filename: "{app}\.env"; Section: "setup"; Key: "DATA_SOURCE"; String: "replay"
Filename: "{app}\.env"; Section: "setup"; Key: "LOG_LEVEL"; String: "INFO"

[UninstallDelete]
; Build caches only. Never target a database, .env, orders, fills, research,
; the per-user Arena home, or paper-database-override.json.
Type: filesandordirs; Name: "{app}\__pycache__"
Type: filesandordirs; Name: "{app}\build"
Type: filesandordirs; Name: "{app}\dist"
Type: files; Name: "{app}\*.pyc"
