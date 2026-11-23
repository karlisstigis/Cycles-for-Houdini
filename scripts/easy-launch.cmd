@echo off
rem SPDX-License-Identifier: Apache-2.0
rem Started by "START HERE - Build Cycles for Houdini.cmd" in the repository root.
rem   easy-launch.cmd <script in scripts\> <pwsh7|any> [arguments...]
rem Finds PowerShell 7 like launchers\Build and make installer.cmd does. With "any" it
rem falls back to the Windows PowerShell that every Windows has (the first run checks the
rem computer there and can install PowerShell 7). Keeps the window open at the end so the
rem result can be read (CYCLES_EASY_NO_PAUSE=1 skips that, for unattended runs).
setlocal
cd /d "%~dp0.."
set "SCRIPT=%~1"
set "MODE=%~2"
set "ARGS="
:collect
if "%~3"=="" goto find
set ARGS=%ARGS% %3
shift /3
goto collect
:find
set "PWSH="
where pwsh >nul 2>nul && set "PWSH=pwsh"
if not defined PWSH if exist "%ProgramFiles%\PowerShell\7\pwsh.exe" set "PWSH=%ProgramFiles%\PowerShell\7\pwsh.exe"
if not defined PWSH for /f "delims=" %%P in ('git config --local --get cycles.pwsh 2^>nul') do if exist "%%P" set "PWSH=%%P"
if not defined PWSH if exist "%LOCALAPPDATA%\Microsoft\WindowsApps\pwsh.exe" set "PWSH=%LOCALAPPDATA%\Microsoft\WindowsApps\pwsh.exe"
if not defined PWSH if /i "%MODE%"=="any" set "PWSH=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not defined PWSH (
  echo.
  echo PowerShell 7 was not found on this computer.
  echo NEXT: install PowerShell 7 from https://aka.ms/powershell, then double-click "START HERE - Build Cycles for Houdini.cmd" again.
  echo.
  if not "%CYCLES_EASY_NO_PAUSE%"=="1" pause
  exit /b 1
)
if exist "%PWSH%" for %%P in ("%PWSH%") do set "PATH=%PATH%;%%~dpP"
"%PWSH%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0%SCRIPT%" %ARGS%
set "CODE=%ERRORLEVEL%"
echo.
if not "%CODE%"=="0" echo This did not finish. The lines marked NOT DONE and NEXT above say why and what to do.
if "%CODE%"=="0" echo You can close this window now.
if not "%CYCLES_EASY_NO_PAUSE%"=="1" pause
exit /b %CODE%
