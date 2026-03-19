@echo off
setlocal EnableExtensions DisableDelayedExpansion

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

call pkg.cmd --action UpdateConfig --pause %*
exit /b %ERRORLEVEL%
