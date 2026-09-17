#define MyAppName "SBR Pager Gateway"
#define MyAppVersion "0.1.0"
#define MyAppPublisher "SBR"
#define MyAppExeName "SBR Pager Gateway.exe"

[Setup]
AppId={{D14B6F72-5100-4F39-986B-A6D045731B0B}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\SBR Pager Gateway
DefaultGroupName=SBR Pager Gateway
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=installer-output
OutputBaseFilename=SBR-Pager-Gateway-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}
SetupLogging=yes

[Languages]
Name: "danish"; MessagesFile: "compiler:Languages\Danish.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Opret genvej på skrivebordet"; GroupDescription: "Genveje:"; Flags: unchecked
Name: "autostart"; Description: "Start SBR Pager Gateway automatisk med Windows"; GroupDescription: "Opstart:"; Flags: unchecked

[Files]
Source: "dist\SBR Pager Gateway\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\SBR Pager Gateway"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\SBR Pager Gateway"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
Name: "{userstartup}\SBR Pager Gateway"; Filename: "{app}\{#MyAppExeName}"; Tasks: autostart

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Start SBR Pager Gateway"; Flags: nowait postinstall skipifsilent
