@echo off
rem SPDX-License-Identifier: Apache-2.0
rem Drag saved scene files here. The converter preserves <name>.pre-res<extension>.
setlocal
cd /d "%~dp0.."
if "%~1"=="" (
  echo Drag .hiplc or .hipnc scene files onto this file to convert them.
  pause
  exit /b 1
)
pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\scripts\run-hython.ps1" "%~dp0..\scripts\convert-rop-resolution.py" %*
if errorlevel 1 (
  echo Conversion failed. Review the error above.
  pause
  exit /b 1
)
echo Finished. Review the conversion report above.
pause
