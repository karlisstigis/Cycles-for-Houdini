@echo off
rem SPDX-License-Identifier: Apache-2.0
rem Double-click: rebuild, qualify isolated runtimes and make an internal candidate.
rem Runs isolated checks and creates an internal candidate; does not activate it.
setlocal
cd /d "%~dp0.."
set "DRY_RUN="
:scan_args
if "%~1"=="" goto args_done
if /i "%~1"=="-DryRun" set "DRY_RUN=1"
shift /1
goto scan_args
:args_done
set "PWSH="
where pwsh >nul 2>nul && set "PWSH=pwsh"
if not defined PWSH if exist "%ProgramFiles%\PowerShell\7\pwsh.exe" set "PWSH=%ProgramFiles%\PowerShell\7\pwsh.exe"
if not defined PWSH for /f "delims=" %%P in ('git config --local --get cycles.pwsh 2^>nul') do if exist "%%P" set "PWSH=%%P"
if not defined PWSH if exist "%LOCALAPPDATA%\Microsoft\WindowsApps\pwsh.exe" set "PWSH=%LOCALAPPDATA%\Microsoft\WindowsApps\pwsh.exe"
if not defined PWSH (
  echo PowerShell 7 was not found.
  if not defined DRY_RUN pause
  exit /b 1
)
if exist "%PWSH%" for %%P in ("%PWSH%") do set "PATH=%PATH%;%%~dpP"
"%PWSH%" -NoProfile -STA -ExecutionPolicy Bypass -File "%~dp0..\scripts\build-release.ps1" %*
if errorlevel 1 (
  echo.
  echo Build or verification failed. No new installer was produced. See the error above.
  if not defined DRY_RUN pause
  exit /b 1
)
if defined DRY_RUN exit /b 0
echo.
echo Finished successfully. Review the release report and candidate shown above.
pause
exit /b 0
