; AXIOM chart terminal - Windows x64 installer (Inno Setup 6).
;
; Paper only. The installer ships the frozen application and its documentation;
; it carries no credentials, installs no service and registers nothing that can
; reach a broker. Every writable thing (config, paper database, logs) lives in
; %LOCALAPPDATA%\AXIOM, which this installer never touches on install, upgrade
; or uninstall unless the operator explicitly answers "yes" when asked.
;
; Build with:  ISCC.exe /DMyAppVersion=3.2.5 axiom.iss

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0-dev"
#endif
#ifndef SourceExe
  #define SourceExe "dist\AXIOM-Chart.exe"
#endif

#define MyAppName "AXIOM Chart Terminal"
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
OutputBaseFilename=AXIOM-Chart-{#MyAppVersion}-windows-x64-setup
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
Source: "{#SourceExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "dist\SOURCE_COMMIT.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "WINDOWS_INSTALL_MANUAL.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\docs\US_EQUITIES_PAPER.md"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "..\..\LICENSE*"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{group}\AXIOM user manual"; Filename: "{app}\WINDOWS_INSTALL_MANUAL.md"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
; The application opens the chart in the default browser itself.
Filename: "{app}\{#MyAppExeName}"; Description: "Start {#MyAppName} (paper mode)"; Flags: nowait postinstall skipifsilent

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
  Answer := MsgBox('Your AXIOM paper account, settings and logs are kept in:'
    + #13#10#13#10 + UserDataDir() + #13#10#13#10
    + 'Do you also want to DELETE that folder?' + #13#10#13#10
    + 'Choose No to keep your paper history for a later reinstall.',
    mbConfirmation, MB_YESNO or MB_DEFBUTTON2);
  DeleteUserData := Answer = IDYES;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if (CurUninstallStep = usPostUninstall) and DeleteUserData then
    DelTree(UserDataDir(), True, True, True);
end;
