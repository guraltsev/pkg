@echo off
setlocal EnableExtensions DisableDelayedExpansion

echo Running gurlatsev/pkg with Machine scope (requires admin)...
echo.

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

call pkg.cmd --scope Machine --pause %*
exit /b %ERRORLEVEL%
