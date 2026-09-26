@echo off
setlocal EnableExtensions DisableDelayedExpansion

rem Select the package-local TUI command before the canonical installed command.
set "GUPKG_TUI_LOCAL=%~dp0gupkg\gupkg-tui.exe"
if not exist "%GUPKG_TUI_LOCAL%" goto :find_system
if exist "%GUPKG_TUI_LOCAL%\NUL" goto :find_system
set "GUPKG_TUI_LOCAL=" & "%GUPKG_TUI_LOCAL%" %*
exit /b %ERRORLEVEL%

:find_system
rem Search PATH entries explicitly, excluding an unrelated executable in cwd.
set "GUPKG_SYSTEM="
if not defined PATH goto :no_system
for %%P in ("%PATH:;=" "%") do if not defined GUPKG_SYSTEM if not "%%~P"=="" if /I not "%%~fP\gupkg.exe"=="%CD%\gupkg.exe" if exist "%%~P\gupkg.exe" if not exist "%%~P\gupkg.exe\NUL" set "GUPKG_SYSTEM=%%~fP\gupkg.exe"
:no_system
if not defined GUPKG_SYSTEM (
    >&2 echo [gupkg] No package-local TUI or PATH gupkg.exe was found.
    exit /b 9009
)

set "GUPKG_SYSTEM=" & "%GUPKG_SYSTEM%" tui %*
exit /b %ERRORLEVEL%
