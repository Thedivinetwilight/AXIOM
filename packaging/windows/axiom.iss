; AXIOM Operator - Windows x64 installer (Inno Setup 6).
;
; Ships local paper and the guarded official Vantage MT5 integration. No
; credential is bundled. Configuration, SQLite journals, logs and DPAPI blobs
; live in %LOCALAPPDATA%\AXIOM, preserved on upgrade and silent uninstall.
;
; Build with:  ISCC.exe /DMyAppVersion=3.7.0a7 axiom.iss

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0-dev"
#endif
; The installer uses the onedir runtime and loaderless mode below.

#define MyAppName "AXIOM Operator"
#define MyAppPublisher "AXIOM"
#define MyAppExeName "AXIOM-Chart.exe"
; Stable identity: an upgrade must replace the previous install, not add a
; second one beside it. Never change this GUID.
#define MyAppId "{{7C1E4A52-9F3B-4E6C-9A18-2B5D7E0C4F31}"

[Setup]
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
; Per-user install: no administrator prompt, and the same account can remove it.
DefaultDirName={autopf}\AXIOM-Chart
DefaultGroupName=AXIOM
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=dist
OutputBaseFilename=AXIOM-Operator-{#MyAppVersion}-windows-x64-no-temp-setup
; Avoid SetupLdr extracting and executing an unsigned setup.tmp from TEMP.
; Keep the generated -0.bin and -1.bin beside the setup executable.
UseSetupLdr=no
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
; Installs over the previous version in the same folder.
UsePreviousAppDir=yes
ChangesAssociations=no
CreateUninstallRegKey=yes
; Documentation shown in the wizard before installation.
InfoBeforeFile=WINDOWS_INSTALL_MANUAL.md

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: checkedonce

[Files]
Source: "dist-onedir\AXIOM-Operator\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "dist\SOURCE_COMMIT.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "WINDOWS_INSTALL_MANUAL.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\USER_MANUAL.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\docs\US_EQUITIES_PAPER.md"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "..\..\docs\VANTAGE_NEXT_VERSION_PLAN.md"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "..\..\docs\TERMINAL_REDESIGN_PLAN.md"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "..\..\docs\TERMINAL_PERFORMANCE.md"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "..\..\docs\LISTED_ASSETS.md"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "..\..\docs\RESEARCH_CANDIDATES.md"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "..\..\docs\WINDOWS_APP_CONTROL.md"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "..\..\docs\BASELINE_AUDIT_AND_ISSUES.md"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "..\..\docs\OWNER_SETUP_GUIDE.md"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "..\..\docs\MODEL_LAB_AND_KRONOS_AUDIT.md"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "..\..\docs\AI_MODEL_LAB_MANUAL.md"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "..\..\docs\RESEARCH_COMPARISON_REPORT.md"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "TERMINAL_BENCHMARK.json"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "TERMINAL_UI_BENCHMARK.json"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "..\..\LICENSE*"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{group}\AXIOM user manual"; Filename: "{app}\WINDOWS_INSTALL_MANUAL.md"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
; Opens the operator console; Vantage remains disconnected and disarmed.
Filename: "{app}\{#MyAppExeName}"; Description: "Start {#MyAppName} (paper default; Vantage disarmed)"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Nothing here: program files are handled by Inno, and the operator's paper
; account is never deleted by this section. See [Code].

[Code]
var
  DeleteUserData: Boolean;

function UserDataDir(): String;
begin
  Result := ExpandConstant('{localappdata}\AXIOM');
end;

function InitializeUninstall(): Boolean;
var
  Answer: Integer;
begin
  DeleteUserData := False;
  Result := True;
  if UninstallSilent then
    exit;  { a silent uninstall always keeps the operator's data }
  if not DirExists(UserDataDir()) then
    exit;
  Answer := MsgBox('Your AXIOM paper state, Vantage journals, protected credentials, settings and logs are kept in:'
    + #13#10#13#10 + UserDataDir() + #13#10#13#10
    + 'Do you also want to DELETE that folder?' + #13#10#13#10
    + 'Choose No to keep all operator data for a later reinstall.',
    mbConfirmation, MB_YESNO or MB_DEFBUTTON2);
  DeleteUserData := Answer = IDYES;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if (CurUninstallStep = usPostUninstall) and DeleteUserData then
    DelTree(UserDataDir(), True, True, True);
end;
