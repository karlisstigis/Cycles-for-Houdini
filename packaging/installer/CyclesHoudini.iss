; SPDX-License-Identifier: Apache-2.0
; Windows installer for Cycles for Houdini. Built by scripts/build-installer.ps1,
; which verifies the release package and passes the defines below.
;
; The installer is a wrapper around the verified package and its own install
; tool (tools/release-package.py inside the package). The tool does the actual
; work: archive verification against the embedded SHA-256, payload hash checks
; and atomic activation of the Houdini package file using Houdini's Python.
; Windows PowerShell handles journal recovery and uninstall, including when
; Houdini has already been removed.
;
; Command-line options (for unattended or test installs):
;   /HFS="<Houdini folder>"  /PREFS="<Houdini preferences folder>"  /REPLACEEXISTING=1
; plus Inno Setup's standard /SILENT, /VERYSILENT, /SUPPRESSMSGBOXES, /DIR=, /LOG=.
; /COMPONENTS="runtime" disables the .blend bridge (and with it the asset browser);
; /COMPONENTS="runtime,blenderbridge" enables the bridge without the asset browser;
; /COMPONENTS="runtime,blenderbridge,blenderbridge\assetbrowser" (or "runtime,*blenderbridge") enables both.
; Without /COMPONENTS, Inno restores the previous Setup's type/components (UsePreviousSetupType and
; UsePreviousComponents); a first install uses the Full type, which includes the asset browser.

#ifndef PackageZip
  #error Define PackageZip (use scripts/build-installer.ps1)
#endif
; A community build (scripts/easy-make-installer.ps1, build-identity.json) passes
; AppName, AppPublisherName, AppPublisherLink and BuildId; the official build passes none of them.
#ifndef AppName
  #define AppName "Cycles for Houdini"
#endif
#ifndef AppPublisherName
  #define AppPublisherName "The Cycles for Houdini Authors"
#endif
#ifndef AppPublisherLink
  #define AppPublisherLink "https://github.com/karlisstigis/Cycles-for-Houdini"
#endif
#ifdef BuildId
  #define BuildIdArgument " --build-id " + BuildId
#else
  #define BuildIdArgument ""
#endif

[Setup]
AppId={{#TargetAppId}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion} (Houdini {#HoudiniVersion})
AppPublisher={#AppPublisherName}
#if AppPublisherLink != ""
AppPublisherURL={#AppPublisherLink}
#endif
VersionInfoVersion={#VersionInfo}
VersionInfoDescription={#AppName} installer
DefaultDirName={localappdata}\{#TargetDirectory}
DisableProgramGroupPage=yes
DisableWelcomePage=no
UsePreviousSetupType=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern dark
SetupIconFile={#StageDir}\branding\installer.ico
WizardImageFile={#StageDir}\branding\installer-welcome.png
WizardImageBackColor=$181818
WizardSmallImageFile={#StageDir}\branding\installer-header.png
WizardSmallImageBackColor=$202020
LicenseFile={#StageDir}\LICENSE.txt
InfoBeforeFile={#StageDir}\BEFORE-INSTALL.txt
OutputDir={#OutputDir}
OutputBaseFilename={#OutputName}
; The package is already a deflated ZIP: further compression gains little.
Compression=lzma2/fast
SolidCompression=no
SetupLogging=yes
UninstallDisplayName={#AppName} (Houdini {#HoudiniVersion})
UninstallDisplayIcon={uninstallexe}
CloseApplications=no

[Messages]
WelcomeLabel2=This will install [name/ver] for Houdini {#HoudiniVersion}.%n%nOpen Houdini sessions keep their previous version until restarted. Other Houdini builds have independent installations.

[Types]
Name: "full"; Description: "Cycles with optional Blender asset bridge and asset browser"
Name: "compact"; Description: "Cycles for native Houdini scenes"
Name: "custom"; Description: "Custom installation"; Flags: iscustom

[Components]
Name: "runtime"; Description: "Cycles renderer (includes Image Iris)"; Types: full compact custom; Flags: fixed
; checkablealone: the bridge stays selectable without its asset browser child.
Name: "blenderbridge"; Description: "Blender .blend asset bridge (requires a separate Blender installation)"; Types: full; Flags: checkablealone
Name: "blenderbridge\assetbrowser"; Description: "Asset browser for Blender asset libraries (dockable panel)"; Types: full

[Files]
; Extracted to a temporary folder and installed by the package's own tool.
Source: "{#PackageZip}"; Flags: dontcopy
Source: "{#StageDir}\release-package-setup.py"; Flags: dontcopy
; Kept for uninstall, repair and registration refresh.
Source: "{#StageDir}\maintain-registration.ps1"; DestDir: "{app}\tools"; Flags: ignoreversion
Source: "{#StageDir}\release-package.py"; DestDir: "{app}\tools"; Flags: ignoreversion
#ifdef HasRefreshRegistration
Source: "{#StageDir}\refresh-registration.ps1"; DestDir: "{app}\tools"; Flags: ignoreversion
Source: "{#StageDir}\Refresh Cycles registration.cmd"; DestDir: "{app}"; Flags: ignoreversion
#endif
Source: "{#StageDir}\LICENSE.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#StageDir}\THIRD_PARTY_NOTICES.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
#ifdef HasRefreshRegistration
Name: "{userprograms}\{#AppName} {#HoudiniVersion}\Refresh registration"; Filename: "{app}\Refresh Cycles registration.cmd"
#endif

[UninstallDelete]
; Immutable versions and mutable data may still be referenced by background
; jobs or contain artist files. The helper deactivates only owned registration.
; Inno removes its own tracked helper/licence files; never recursively delete
; arbitrary content or retained versions from a custom application directory.
Type: files; Name: "{app}\install.log"
Type: files; Name: "{app}\houdini-python.txt"
Type: dirifempty; Name: "{app}"

[Code]
const
  PackageFile = '{#TargetPackageFile}';
  InstallerMutex = 'Global\CyclesForHoudiniInstaller-{#TargetAppId}';
var
  HoudiniPage: TInputDirWizardPage;
  InstallLog: String;
  SetupTransaction: Boolean;
  SetupToken: String;
  InstallerLease: THandle;

function CreateInstallerMutex(Attributes, InitialOwner: LongWord; Name: String): THandle;
external 'CreateMutexW@{sys}\kernel32.dll stdcall delayload';

function WaitForInstallerMutex(Mutex: THandle; Milliseconds: LongWord): LongWord;
external 'WaitForSingleObject@{sys}\kernel32.dll stdcall delayload';

function ReleaseInstallerMutex(Mutex: THandle): LongBool;
external 'ReleaseMutex@{sys}\kernel32.dll stdcall delayload';

function CloseInstallerMutex(Mutex: THandle): LongBool;
external 'CloseHandle@{sys}\kernel32.dll stdcall delayload';

function AcquireInstallerLease: Boolean;
var
  WaitStatus: LongWord;
begin
  Result := False;
  InstallerLease := CreateInstallerMutex(0, 0, InstallerMutex);
  if InstallerLease <> 0 then begin
    WaitStatus := WaitForInstallerMutex(InstallerLease, 0);
    // WAIT_OBJECT_0 or WAIT_ABANDONED: in both cases this thread owns the mutex.
    Result := (WaitStatus = 0) or (WaitStatus = $80);
    if not Result then begin
      CloseInstallerMutex(InstallerLease);
      InstallerLease := 0;
    end;
  end;
  if not Result then
    SuppressibleMsgBox('Another Setup or uninstall may be working on this build. Close it and try again.',
      mbError, MB_OK, IDOK);
end;

procedure ReleaseInstallerLease;
begin
  if InstallerLease <> 0 then begin
    ReleaseInstallerMutex(InstallerLease);
    CloseInstallerMutex(InstallerLease);
    InstallerLease := 0;
  end;
end;

function InitializeSetup: Boolean;
begin
  Result := AcquireInstallerLease;
end;

function ParamOr(Name, Default: String): String;
begin
  Result := ExpandConstant('{param:' + Name + '|}');
  if Result = '' then
    Result := Default;
end;

function DefaultHoudini: String;
begin
  if not RegQueryStringValue(HKLM64, 'SOFTWARE\Side Effects Software\Houdini {#HoudiniVersion}', 'InstallPath', Result) then
    Result := ExpandConstant('{commonpf64}\Side Effects Software\Houdini {#HoudiniVersion}');
end;

function DefaultPreferences: String;
begin
  Result := GetEnv('HOUDINI_USER_PREF_DIR');
  if Result <> '' then
    StringChangeEx(Result, '__HVER__', '{#HoudiniPrefVersion}', True)
  else
    Result := ExpandConstant('{userdocs}\houdini{#HoudiniPrefVersion}');
end;

function HoudiniPython(Hfs: String): String;
begin
  Result := AddBackslash(Hfs) + 'python313\python.exe';
end;

function ProcessRunning(Name, HostRoot, AppRoot: String): Boolean;
var
  Locator, Service, Items, Item: Variant;
  I: Integer;
  Path: String;
begin
  Result := False;
  try
    Locator := CreateOleObject('WbemScripting.SWbemLocator');
    Service := Locator.ConnectServer('.', 'root\CIMV2');
    Items := Service.ExecQuery('SELECT ExecutablePath FROM Win32_Process WHERE Name="' + Name + '"');
    for I := 0 to Items.Count - 1 do begin
      Item := Items.ItemIndex(I);
      if not VarIsNull(Item.ExecutablePath) then begin
        Path := Lowercase(String(Item.ExecutablePath));
        if (Pos(Lowercase(AddBackslash(HostRoot)), Path) = 1) or
           (Pos(Lowercase(AddBackslash(AppRoot)), Path) = 1) then begin
          Result := True;
          exit;
        end;
      end else begin
        // An unidentifiable matching process may hold the target's payload.
        Result := True;
        exit;
      end;
    end;
  except
  end;
end;

function HoudiniRunning(HostRoot: String): Boolean;
var
  AppRoot: String;
begin
  AppRoot := ExpandConstant('{app}');
  Result := ProcessRunning('houdini.exe', HostRoot, AppRoot) or ProcessRunning('houdinifx.exe', HostRoot, AppRoot) or
            ProcessRunning('houdinicore.exe', HostRoot, AppRoot) or ProcessRunning('hython.exe', HostRoot, AppRoot) or
            ProcessRunning('husk.exe', HostRoot, AppRoot) or ProcessRunning('ch_worker.exe', HostRoot, AppRoot);
end;

function LogText(Path: String): String;
var
  Text: AnsiString;
begin
  if LoadStringFromFile(Path, Text) then
    Result := Trim(Utf8Decode(Text))
  else
    Result := '(no output)';
  if Length(Result) > 1500 then
    Result := '...' + Copy(Result, Length(Result) - 1500, 1501);
end;

// Run Houdini's Python on the package tool; output goes to Log.
function RunTool(Python, Tool, Arguments, Log: String; var Code: Integer): Boolean;
begin
  Result := Exec(Python, '-I "' + Tool + '" --log "' + Log + '" ' + Arguments,
                 '', SW_HIDE, ewWaitUntilTerminated, Code);
end;

function RunMaintenance(Action, Tool, LogPath: String; var Code: Integer): Boolean;
begin
  Result := Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
    '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + Tool +
    '" -Action ' + Action + ' -ApplicationRoot "' + ExpandConstant('{app}') +
    '" -AppId "{#TargetAppId}" -Log "' + LogPath + '" -WizardLeaseHeld',
    '', SW_HIDE, ewWaitUntilTerminated, Code);
end;

procedure InitializeWizard;
begin
  { The wordmark is wide. Give it a wide header slot rather than stretching
    it into Inno's default square, and keep the native page text clear. }
  WizardForm.WizardSmallBitmapImage.Width := WizardForm.WizardSmallBitmapImage.Height * 3;
  WizardForm.WizardSmallBitmapImage.Left := WizardForm.MainPanel.Width - WizardForm.WizardSmallBitmapImage.Width - ScaleX(12);
  WizardForm.PageNameLabel.Width := WizardForm.WizardSmallBitmapImage.Left - WizardForm.PageNameLabel.Left - ScaleX(12);
  WizardForm.PageDescriptionLabel.Width := WizardForm.PageNameLabel.Width;
  HoudiniPage := CreateInputDirPage(wpSelectDir, 'Houdini',
    'Which Houdini should load Cycles?',
    'This build is for Houdini {#HoudiniVersion} exactly. Choose its installation folder and the Houdini preferences folder that ' +
    'receives the package file (packages\' + PackageFile + ').', False, '');
  HoudiniPage.Add('Houdini {#HoudiniVersion} installation folder:');
  HoudiniPage.Add('Houdini preferences folder:');
  HoudiniPage.Values[0] := ParamOr('HFS', GetPreviousData('HFS', DefaultHoudini));
  HoudiniPage.Values[1] := ParamOr('PREFS', GetPreviousData('PREFS', DefaultPreferences));
end;

procedure RegisterPreviousData(PreviousDataKey: Integer);
begin
  SetPreviousData(PreviousDataKey, 'HFS', HoudiniPage.Values[0]);
  SetPreviousData(PreviousDataKey, 'PREFS', HoudiniPage.Values[1]);
end;

// The Houdini build in a folder, from the HDK header Cycles also checks when
// it loads (the toolkit is part of a default Houdini installation).
function HoudiniBuild(Hfs: String): String;
var
  Lines: TArrayOfString;
  I, First, Last: Integer;
begin
  Result := '';
  if not LoadStringsFromFile(AddBackslash(Hfs) + 'toolkit\include\SYS\SYS_Version.h', Lines) then
    exit;
  for I := 0 to GetArrayLength(Lines) - 1 do
    if Pos('#define SYS_VERSION_FULL "', Lines[I]) = 1 then begin
      First := Pos('"', Lines[I]) + 1;
      Last := Length(Lines[I]);
      while (Last > First) and (Lines[I][Last] <> '"') do
        Last := Last - 1;
      Result := Copy(Lines[I], First, Last - First);
      exit;
    end;
end;

// Empty when the selection is usable; otherwise the reason it is not.
function HoudiniProblem: String;
var
  Hfs, Build: String;
begin
  Result := '';
  Hfs := HoudiniPage.Values[0];
  if not FileExists(AddBackslash(Hfs) + 'bin\houdini.exe') or not FileExists(HoudiniPython(Hfs)) then
    Result := 'This folder is not a Houdini installation (bin\houdini.exe and python313\python.exe are required):' + #13#10 + Hfs
  else begin
    Build := HoudiniBuild(Hfs);
    if Build = '' then
      Result := 'The Houdini build in this folder could not be identified: toolkit\include\SYS\SYS_Version.h is missing. ' +
        'Cycles needs Houdini''s standard toolkit files; reinstall Houdini {#HoudiniVersion} with its default components.'
    else if Build <> '{#HoudiniVersion}' then
      Result := 'This Cycles build is for Houdini {#HoudiniVersion}; the selected folder is Houdini ' + Build + '.' + #13#10#13#10 +
        'Cycles only loads in the exact Houdini build it was made for. Select a Houdini {#HoudiniVersion} installation, ' +
        'or use a Cycles installer made for Houdini ' + Build + '.';
  end;
  if (Result = '') and (Trim(HoudiniPage.Values[1]) = '') then
    Result := 'Choose the Houdini preferences folder.';
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Problem: String;
begin
  Result := True;
  if CurPageID = HoudiniPage.ID then begin
    Problem := HoudiniProblem;
    if Problem <> '' then begin
      SuppressibleMsgBox(Problem, mbError, MB_OK, IDOK);
      Result := False;
    end;
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Python, Root, Prefs, Arguments, RecoveryLog: String;
  Code: Integer;
begin
  // Also checked here: unattended installs do not visit the wizard pages.
  Result := HoudiniProblem;
  if Result <> '' then
    exit;
  if HoudiniRunning(HoudiniPage.Values[0]) then
    if SuppressibleMsgBox('Houdini (or a Cycles render worker) is running. Open sessions keep their current version until ' +
         'restarted. Install anyway?', mbConfirmation, MB_YESNO, IDYES) <> IDYES then begin
      Result := 'Close Houdini and run Setup again.';
      exit;
    end;
  Python := HoudiniPython(HoudiniPage.Values[0]);
  Root := ExpandConstant('{app}\installation');
  Prefs := HoudiniPage.Values[1];
  Arguments := 'install "' + ExpandConstant('{tmp}\{#PackageZipName}') +
    '" --install-root "' + Root + '" --preference-root "' + Prefs + '" --data-root "' + ExpandConstant('{app}\data') +
    '" --application-root "' + ExpandConstant('{app}') + '" --expected-sha256 {#PackageSha} --allow-internal --defer-commit' +
    ' --setup-token "' + ExpandConstant('{tmp}') + '"';
  if WizardIsComponentSelected('blenderbridge') then
    Arguments := Arguments + ' --blender-bridge enabled'
  else
    Arguments := Arguments + ' --blender-bridge disabled';
  // Inno never selects a child without its parent; check both anyway so the
  // helper never receives an enabled browser with a disabled bridge.
  if WizardIsComponentSelected('blenderbridge') and WizardIsComponentSelected('blenderbridge\assetbrowser') then
    Arguments := Arguments + ' --asset-browser enabled'
  else
    Arguments := Arguments + ' --asset-browser disabled';
  if FileExists(AddBackslash(Root) + '.cycles-houdini-installation.json') then begin
    ExtractTemporaryFile('maintain-registration.ps1');
    RecoveryLog := ExpandConstant('{tmp}\recovery.log');
    if not RunMaintenance('Recover', ExpandConstant('{tmp}\maintain-registration.ps1'), RecoveryLog, Code)
        or (Code <> 0) then begin
      Result := 'Setup stopped while recovering the previous installation:' + #13#10#13#10 + LogText(RecoveryLog);
      exit;
    end;
  end;
  ExtractTemporaryFile('release-package-setup.py');
  ExtractTemporaryFile('{#PackageZipName}');
  InstallLog := ExpandConstant('{tmp}\install.log');
  // Ask the helper about active registration ownership. An inactive retained
  // state file does not own the original package restored by uninstall.
  // Exit 2 requests consent; any invalid/foreign ownership is a hard refusal.
  if not RunTool(Python, ExpandConstant('{tmp}\release-package-setup.py'),
       'registration-status --houdini {#HoudiniVersion} --install-root "' + Root +
       '" --preference-root "' + Prefs + '" --data-root "' + ExpandConstant('{app}\data') +
#if defined(HasRefreshRegistration) || defined(BuildId)
       '" --application-root "' + ExpandConstant('{app}') + '" --archive "' +
       ExpandConstant('{tmp}\{#PackageZipName}') + '"{#BuildIdArgument}', InstallLog, Code) then begin
#else
       '" --application-root "' + ExpandConstant('{app}') + '"', InstallLog, Code) then begin
#endif
    Result := 'Could not start Houdini''s Python: ' + Python;
    exit;
  end;
  if (Code <> 0) and (Code <> 2) then begin
    Result := 'Checking package registration failed:' + #13#10#13#10 + LogText(InstallLog);
    exit;
  end;
  // An unmanaged file is replaced only with consent; its exact bytes are
  // restored on uninstall. Install rechecks ownership before activation.
  if Code = 2 then begin
    if ExpandConstant('{param:REPLACEEXISTING|0}') <> '0' then
      Arguments := Arguments + ' --replace-existing'
    else if SuppressibleMsgBox('The preferences folder already has packages\' + PackageFile + ' that this installer did not create. ' +
         'Replace it? The original is restored when you uninstall.', mbConfirmation, MB_YESNO, IDNO) = IDYES then
      Arguments := Arguments + ' --replace-existing'
    else begin
      Result := 'Installation cancelled: the existing package file was kept.';
      exit;
    end;
  end;
  WizardForm.StatusLabel.Caption := 'Verifying and installing the Cycles package...';
  SetupTransaction := True;
  SetupToken := ExpandConstant('{tmp}');
  if not RunTool(Python, ExpandConstant('{tmp}\release-package-setup.py'), Arguments, InstallLog, Code) then
    Result := 'Could not start Houdini''s Python: ' + Python
  else if Code <> 0 then
    Result := 'Installing the package failed (exit code ' + IntToStr(Code) + '):' + #13#10#13#10 + LogText(InstallLog);
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Code: Integer;
  Log: String;
begin
  if CurStep = ssPostInstall then begin
    FileCopy(InstallLog, ExpandConstant('{app}\install.log'), False);
    SaveStringToFile(ExpandConstant('{app}\houdini-python.txt'), Utf8Encode(HoudiniPython(HoudiniPage.Values[0])), False);
  end;
  if CurStep = ssDone then begin
    Log := ExpandConstant('{tmp}\commit.log');
    if not RunTool(HoudiniPython(HoudiniPage.Values[0]), ExpandConstant('{tmp}\release-package-setup.py'),
         'commit-setup --install-root "' + ExpandConstant('{app}\installation') + '" --setup-token "' + SetupToken + '"', Log, Code) or (Code <> 0) then
      RaiseException('Could not commit the verified installation: ' + LogText(Log));
    SetupTransaction := False;
  end;
end;

procedure DeinitializeSetup;
var
  Code: Integer;
  Log: String;
begin
  try
    if SetupTransaction then begin
      Log := ExpandConstant('{tmp}\rollback.log');
      if not RunTool(HoudiniPython(HoudiniPage.Values[0]), ExpandConstant('{tmp}\release-package-setup.py'),
           'abort-setup --install-root "' + ExpandConstant('{app}\installation') + '" --setup-token "' + SetupToken + '"', Log, Code) or (Code <> 0) then
        SuppressibleMsgBox('Run this Setup again to recover the interrupted installation: ' + LogText(Log), mbError, MB_OK, IDOK);
    end;
  finally
    ReleaseInstallerLease;
  end;
end;

function InitializeUninstall: Boolean;
begin
  Result := AcquireInstallerLease;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  MaintenanceLog: String;
  Code: Integer;
begin
  if CurUninstallStep <> usUninstall then
    exit;
  // Deactivate only after confirmation and before Inno removes the helper.
  MaintenanceLog := ExpandConstant('{app}\uninstall-{#HoudiniVersion}.log');
  if not RunMaintenance('Uninstall', ExpandConstant('{app}\tools\maintain-registration.ps1'), MaintenanceLog, Code)
      or (Code <> 0) then
    RaiseException('Deactivating the Houdini package failed:' + #13#10#13#10 + LogText(MaintenanceLog));
end;

procedure DeinitializeUninstall;
begin
  ReleaseInstallerLease;
end;
