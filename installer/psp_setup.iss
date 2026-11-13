; PSP 媒体管理器 安装包脚本
; 用 Inno Setup 6 编译：ISCC.exe psp_setup.iss

#define MyAppName "PSP媒体管理器"
#define MyAppVersion "1.0.3"
#define MyAppExeName "PSP媒体管理器.exe"
#define MySource "C:\Users\Admin\Doubao\chats\2026-11-12\new-chat\发布\PSP媒体管理器"
#define MyIcon "C:\Users\Admin\Doubao\chats\2026-11-12\new-chat\app.ico"
#define MyNotice "C:\Users\Admin\Doubao\chats\2026-11-12\new-chat\说明.txt"
#define MyLang "C:\Users\Admin\Doubao\chats\2026-11-12\new-chat\发布\ChineseSimplified.isl"
#define MyOutDir "C:\Users\Admin\Doubao\chats\2026-11-12\new-chat\发布"

[Setup]
AppId={{22A46D5D-DD07-4D3A-B8C5-FF2159AB1CD8}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher=PSP媒体管理器
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir={#MyOutDir}
OutputBaseFilename=PSP媒体管理器_v{#MyAppVersion}_安装程序
SetupIconFile={#MyIcon}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
ShowLanguageDialog=no

[Languages]
Name: "chs"; MessagesFile: "{#MyLang}"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#MySource}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#MyNotice}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent
