@echo off
setlocal EnableExtensions DisableDelayedExpansion

echo Running pkg with User scope...
echo.

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

call pkg.cmd --pause %*
exit /b %ERRORLEVEL%
