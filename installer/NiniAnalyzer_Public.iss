; NiniAnalyzer Public installer (Inno Setup 6.x)
; Build: Build_Public_EXE.cmd runs this automatically when ISCC.exe is installed.
; Installs per-user (no admin prompt). Raid records in %LOCALAPPDATA%\NiniAnalyzer\Public are never removed.

#define AppName "NiniAnalyzer Public"
#define AppVersion "1.0.9"
#define AppExe "NiniAnalyzer_Public.exe"
#define AppUserModelID "NINI.Analyzer.Public"

[Setup]
; KEEP THIS AppId FOREVER. Every future version must use the same value so a new
; installer upgrades the existing install instead of adding a second program.
AppId={{3CC9A534-B02D-467D-909C-497608E24CD7}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Nini
DefaultDirName={localappdata}\Programs\NiniAnalyzer Public
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=NiniAnalyzer_Public_{#AppVersion}_Setup_Windows_x64
SetupIconFile=..\resources\brand_public\nini_analyzer.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"

[Tasks]
Name: "desktopicon"; Description: "바탕화면에 바로가기 만들기"; GroupDescription: "추가 작업:"

[InstallDelete]
; Remove the previous version's bundled libraries before copying the new build
; (prevents stale DLL/resource mixes). User data lives elsewhere and is untouched.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\dist\NiniAnalyzer_Public\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; AppUserModelID: "{#AppUserModelID}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; AppUserModelID: "{#AppUserModelID}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{#AppName} 실행"; Flags: nowait postinstall skipifsilent
