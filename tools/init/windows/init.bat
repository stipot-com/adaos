@echo off
setlocal EnableExtensions EnableDelayedExpansion

rem Minimal bootstrap entrypoint (Windows CMD).
rem Served from GitHub raw:
rem   https://raw.githubusercontent.com/stipot-com/adaos/rev2026/tools/init/windows/init.bat
rem It downloads and runs init.ps1 (PowerShell) so users can double-click a single file.
rem Zone arguments are passed through as-is, for example: init.bat -ZoneId ru

set "URL=https://raw.githubusercontent.com/stipot-com/adaos/rev2026/tools/init/windows/init.ps1"
set "TMP=%TEMP%\\adaos_init_%RANDOM%_%RANDOM%.ps1"

echo [*] Downloading %URL%
powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch {}; iwr -UseBasicParsing -Uri '%URL%' -OutFile '%TMP%'" || goto :err

echo [*] Running init.ps1
powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%TMP%" %*
set "RC=%ERRORLEVEL%"
del /f /q "%TMP%" >nul 2>&1
exit /b %RC%

:err
echo [x] Failed to download init.ps1
exit /b 1
