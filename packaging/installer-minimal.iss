; Minimal AXIOM installer for CI debugging
#ifndef ArenaExe
  #error "ArenaExe must be defined"
#endif
#ifndef AppVersion
  #define AppVersion "3.2.4"
#endif
#define MyAppName "AXIOM Arena"
#define MyAppExeName "Arena.exe"

[Setup]
AppId={{B8E5C7A2-4F31-4D29-9E8A-1A2B3C4D5E6F}
AppName={#MyAppName}
AppVersion={#AppVersion}
DefaultDirName={autopf}\AXIOM
DefaultGroupName={#MyAppName}
OutputBaseFilename=AXIOM-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
CloseApplications=no
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "{#ArenaExe}"; DestDir: "{app}"; Flags: ignoreversion

[Dirs]
Name: "{app}\data"; Flags: uninsneveruninstall
Name: "{app}\logs"; Flags: uninsneveruninstall

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"