; Inno Setup script: builds dist\PhotobandSetup-<version>.exe from dist\Photoband (PyInstaller output).
; Normally run by build.ps1, which passes:
;   /DAppVersion=<photoband.__version__>   (single source of the version)
;   /DWebView2Bootstrapper=<path to MicrosoftEdgeWebview2Setup.exe>
;   /Sphotoband=<signtool command> /DSign=1   (only when signing is configured)
#define AppName "Photoband"
#ifndef AppDir
  #define AppDir "..\..\dist\Photoband"
#endif
; the oldest Windows the build runs on: 10.0 (default build, Python 3.12) or 6.3 (the -Target win81 build)
#ifndef MinWinVersion
  #define MinWinVersion "10.0"
#endif
#ifndef OutputSuffix
  #define OutputSuffix ""
#endif
#ifndef AppVersion
  #error AppVersion is not defined. Build with packaging\windows\build.ps1 (or pass /DAppVersion=x.y.z).
#endif

[Setup]
AppId={{6C3E1D6E-6F0B-4B7B-9C1F-5B1B8C2E7A11}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Photoband
; Per-user install by default: no UAC prompt, goes to %LOCALAPPDATA%\Programs\Photoband.
; The dialog lets someone with admin rights install for all users instead.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\..\dist
OutputBaseFilename=PhotobandSetup-{#AppVersion}{#OutputSuffix}
MinVersion={#MinWinVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\Photoband.exe
CloseApplications=yes
#ifdef Sign
SignTool=photoband
SignedUninstaller=yes
#endif

[Files]
Source: "{#AppDir}\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion
#ifdef WebView2Bootstrapper
Source: "{#WebView2Bootstrapper}"; DestDir: "{tmp}"; DestName: "MicrosoftEdgeWebview2Setup.exe"; Flags: deleteafterinstall; Check: NeedsWebView2
#endif

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\Photoband.exe"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\Photoband.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Run]
#ifdef WebView2Bootstrapper
; Without WebView2 the app still works: it opens in the default browser instead.
Filename: "{tmp}\MicrosoftEdgeWebview2Setup.exe"; Parameters: "/silent /install"; StatusMsg: "Installing Microsoft Edge WebView2 Runtime..."; Check: NeedsWebView2; Flags: waituntilterminated
#endif
Filename: "{app}\Photoband.exe"; Description: "Launch Photoband"; Flags: nowait postinstall skipifsilent

[Code]
const
  WebView2Key = 'Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';

function HasWebView2(RootKey: Integer; SubKey: String): Boolean;
var
  Version: String;
begin
  Result := RegQueryStringValue(RootKey, SubKey, 'pv', Version) and (Version <> '') and (Version <> '0.0.0.0');
end;

function NeedsWebView2(): Boolean;
begin
  Result := not (HasWebView2(HKLM, 'SOFTWARE\WOW6432Node\' + WebView2Key)
                 or HasWebView2(HKLM, 'SOFTWARE\' + WebView2Key)
                 or HasWebView2(HKCU, 'Software\' + WebView2Key));
end;
