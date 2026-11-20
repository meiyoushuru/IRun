; SPDX-License-Identifier: MPL-2.0
#ifndef AppVersion
  #define AppVersion "1.2.0"
#endif
#ifndef FileVersion
  #define FileVersion "1.2.0.0"
#endif
#ifndef BundleDir
  #define BundleDir "..\dist\IRun"
#endif
#ifndef ReleaseDir
  #define ReleaseDir "..\release"
#endif
#ifndef EditionSuffix
  #define EditionSuffix ""
#endif
[Setup]
AppId={{BF65DE62-0A7C-48A4-B1E4-09B86F0B9457}
AppName=IRun
AppVersion={#AppVersion}
AppPublisher=IRun contributors
AppPublisherURL=https://github.com/meiyoushuru/IRun
DefaultDirName={autopf}\IRun
UsePreviousAppDir=yes
UsePreviousTasks=no
DefaultGroupName=IRun
DisableProgramGroupPage=yes
AllowNoIcons=yes
OutputDir={#ReleaseDir}
OutputBaseFilename=IRun-{#AppVersion}{#EditionSuffix}-Setup-x64
SetupIconFile=..\assets\irun.ico
UninstallDisplayIcon={app}\IRun.exe
LicenseFile=..\LICENSE
ArchitecturesAllowed=x64os
ArchitecturesInstallIn64BitMode=x64os
PrivilegesRequired=admin
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
CloseApplicationsFilter=*.exe,*.dll,*.pyd
RestartApplications=no
MinVersion=10.0.17763
VersionInfoVersion={#FileVersion}
VersionInfoProductName=IRun

[Languages]
Name: "chinesesimp"; MessagesFile: "compiler:Default.isl,ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Messages]
english.BeveledLabel=IRun · iPhone virtual running

[CustomMessages]
english.DesktopShortcut=Create a desktop shortcut
chinesesimp.DesktopShortcut=创建桌面快捷方式
english.LaunchIRun=Launch IRun
chinesesimp.LaunchIRun=启动 IRun
english.PreparingApple=Setting up Apple USB support on this PC. Please wait a few minutes...
chinesesimp.PreparingApple=正在电脑上配置 Apple USB 连接组件，可能需要几分钟，请稍候…
english.AppleIncomplete=Apple USB setup failed on this PC. Check your internet connection and run the IRun installer again. Setup log:
chinesesimp.AppleIncomplete=电脑未能完成 Apple USB 组件配置。请检查电脑网络后重新运行 IRun 安装包。安装日志见下方。

[Tasks]
Name: "desktopicon"; Description: "{cm:DesktopShortcut}"

[Files]
Source: "{#BundleDir}\*"; DestDir: "{app}"; Excludes: "routes\*"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
Type: files; Name: "{app}\_internal\assets\offline-maps.zip"
Type: files; Name: "{app}\_internal\assets\maps\campus-osm.zip"
Type: files; Name: "{app}\VRun.exe"
Type: filesandordirs; Name: "{app}\_internal\DeveloperDiskImage"
Type: files; Name: "{app}\_internal\libimobiledevice\win\idevicedevmodectl.exe"
Type: files; Name: "{app}\_internal\libimobiledevice\win\ideviceimagemounter.exe"
Type: files; Name: "{app}\_internal\libimobiledevice\win\idevicepair.exe"
Type: files; Name: "{app}\_internal\libimobiledevice\win\idevicesetlocation.exe"
Type: files; Name: "{app}\routes\HNroute.txt"
Type: files; Name: "{app}\routes\ZJG-east.txt"
Type: files; Name: "{app}\routes\ZJG-west.txt"

[Icons]
Name: "{group}\IRun"; Filename: "{app}\IRun.exe"; AppUserModelID: "IRun.Desktop"
Name: "{autodesktop}\IRun"; Filename: "{app}\IRun.exe"; AppUserModelID: "IRun.Desktop"; Tasks: desktopicon

[Registry]
Root: HKA; Subkey: "Software\Classes\AppUserModelId\IRun.Desktop"; ValueType: string; ValueName: "DisplayName"; ValueData: "IRun"; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\AppUserModelId\IRun.Desktop"; ValueType: string; ValueName: "IconUri"; ValueData: "{app}\_internal\assets\irun.ico"

[Run]
Filename: "{app}\IRun.exe"; Description: "{cm:LaunchIRun}"; Flags: nowait postinstall skipifsilent runasoriginaluser; Check: CanLaunchIRun

[Code]
var
  DriverRestart: Boolean;
  DriverFailed: Boolean;

function CanLaunchIRun(): Boolean;
begin
  Result := not DriverFailed and not DriverRestart;
end;

function GetCustomSetupExitCode(): Integer;
begin
  if DriverFailed then
    Result := 20
  else if DriverRestart then
    Result := 3010
  else
    Result := 0;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
begin
  if CurStep = ssPostInstall then
  begin
    WizardForm.StatusLabel.Caption := CustomMessage('PreparingApple');
    if not Exec(ExpandConstant('{app}\IRun.exe'), '--install-drivers', '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
      DriverFailed := True
    else if ResultCode = 3010 then
      DriverRestart := True
    else if ResultCode <> 0 then
      DriverFailed := True;
    if DriverFailed then
    begin
      Log('Apple USB prerequisites failed');
      SuppressibleMsgBox(CustomMessage('AppleIncomplete') + #13#10 +
        ExpandConstant('{commonappdata}\IRun\driver-install.log'), mbError, MB_OK, IDOK);
    end;
  end;
end;

function NeedRestart(): Boolean;
begin
  Result := DriverRestart;
end;
