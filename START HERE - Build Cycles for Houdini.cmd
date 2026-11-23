@echo off
rem SPDX-License-Identifier: Apache-2.0
rem The one file to double-click. First run: checks this computer, installs what is missing
rem (after asking once), gets the sources, builds Cycles for Houdini, runs the quick tests and
rem makes an installer for each Houdini build (it installs nothing itself). Later runs: a short
rem menu. See scripts\easy-start.ps1.
call "%~dp0scripts\easy-launch.cmd" easy-start.ps1 any %*
exit /b %ERRORLEVEL%
