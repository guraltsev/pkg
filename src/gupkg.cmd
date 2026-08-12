@echo off
setlocal EnableExtensions DisableDelayedExpansion
rem Repository convenience launcher. Installed users should run gupkg directly.
set "GUPKG_PYTHON_FILE=%~dp0gupkg.python"
if not defined GUPKG_PYTHON if exist "%GUPKG_PYTHON_FILE%" set /p GUPKG_PYTHON=<"%GUPKG_PYTHON_FILE%"
if not defined GUPKG_PYTHON set "GUPKG_PYTHON=python"
pushd "%~dp0"
"%GUPKG_PYTHON%" -m gupkg %*
set "GUPKG_EXIT_CODE=%ERRORLEVEL%"
popd
exit /b %GUPKG_EXIT_CODE%
