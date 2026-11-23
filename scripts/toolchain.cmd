@echo off
rem SPDX-License-Identifier: Apache-2.0
setlocal
set "VS_ROOT=%~1"
if not defined VS_ROOT set "VS_ROOT=%ProgramFiles(x86)%\Microsoft Visual Studio\2022\BuildTools"
rem SPDX-License-Identifier: Apache-2.0
call "%VS_ROOT%\Common7\Tools\VsDevCmd.bat" -no_logo -arch=x64 -host_arch=x64 -vcvars_ver=14.42 >nul
if errorlevel 1 exit /b %errorlevel%
set

