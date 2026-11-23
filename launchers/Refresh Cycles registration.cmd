@echo off
rem SPDX-License-Identifier: Apache-2.0
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\refresh-registration.ps1" -PromptForHoudiniRoot
set "refresh_exit=%ERRORLEVEL%"
pause
exit /b %refresh_exit%
