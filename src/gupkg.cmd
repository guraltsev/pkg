@echo off
setlocal EnableExtensions DisableDelayedExpansion
rem Repository convenience launcher. Installed users should run gupkg directly.
set "GUPKG_PYTHON_FILE=%~dp0gupkg.python"
if not defined GUPKG_PYTHON if exist "%GUPKG_PYTHON_FILE%" set /p GUPKG_PYTHON=<"%GUPKG_PYTHON_FILE%"
if not defined GUPKG_PYTHON set "GUPKG_PYTHON=python"
"%GUPKG_PYTHON%" -m gupkg %*
exit /b %ERRORLEVEL%
