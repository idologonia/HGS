; Inno Setup script (free: https://jrsoftware.org/isinfo.php). Built by CI after PyInstaller.
; Installs per user (no administrator rights needed).
#define AppVersion GetEnv("HGS_VERSION")
#if AppVersion == ""
  #define AppVersion "0.1.3"
#endif

[Setup]
AppId={{7C3E3E8B-6B8E-4F0C-9C61-2A4B1E5D7F10}
AppName=HOME GAME SERVER
AppVersion={#AppVersion}
AppPublisher=HGS
DefaultDirName={localappdata}\Programs\HGS
DefaultGroupName=HOME GAME SERVER
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=HGS-Setup-{#AppVersion}
SetupIconFile=hgs.ico
UninstallDisplayIcon={app}\HGS.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
DisableProgramGroupPage=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "ukrainian"; MessagesFile: "compiler:Languages\Ukrainian.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\HGS\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{autoprograms}\HOME GAME SERVER"; Filename: "{app}\HGS.exe"
Name: "{autodesktop}\HOME GAME SERVER"; Filename: "{app}\HGS.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\HGS.exe"; Description: "{cm:LaunchProgram,HOME GAME SERVER}"; Flags: nowait postinstall skipifsilent
; Your data (%LOCALAPPDATA%\HGS) is kept on uninstall.
