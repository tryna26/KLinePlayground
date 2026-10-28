#ifndef AppVersion
  #define AppVersion "4.0.0-rc.1"
#endif
#define SourceRoot AddBackslash(SourcePath) + ".."
[Setup]
AppId={{EF3868C0-739F-418F-9A72-A8934B4D3F51}
AppName=KLinePlayground 组合训练
AppVersion={#AppVersion}
AppPublisher=tryna26; based on fat7/KLinePlayground
AppPublisherURL=https://github.com/tryna26/KLinePlayground
DefaultDirName={localappdata}\Programs\KLinePlayground
DefaultGroupName=KLinePlayground
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#SourceRoot}\dist\release
OutputBaseFilename=KLinePlayground-{#AppVersion}-windows-x64-setup
SetupIconFile={#SourceRoot}\frontend\assets\favicon.ico
UninstallDisplayIcon={app}\bin\KLinePlayground.exe
LicenseFile={#SourceRoot}\LICENSE
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
SetupLogging=yes
[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "chinesesimplified"; MessagesFile: "{#SourcePath}\ChineseSimplified.isl"
[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; Flags: unchecked
[Files]
Source: "{#SourceRoot}\dist\KLinePlayground\*"; DestDir: "{app}\bin"; Flags: ignoreversion recursesubdirs createallsubdirs
[Icons]
Name: "{group}\KLinePlayground"; Filename: "{app}\bin\KLinePlayground.exe"
Name: "{group}\组合训练"; Filename: "{app}\bin\KLinePlayground.exe"; Parameters: "--portfolio"
Name: "{group}\卸载 KLinePlayground"; Filename: "{uninstallexe}"
Name: "{autodesktop}\KLinePlayground"; Filename: "{app}\bin\KLinePlayground.exe"; Tasks: desktopicon
[Run]
Filename: "{app}\bin\KLinePlayground.exe"; Description: "启动 KLinePlayground"; Flags: nowait postinstall skipifsilent
; Runtime data/users are created by the app, never shipped or recursively deleted.
