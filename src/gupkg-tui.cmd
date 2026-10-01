@echo off
setlocal EnableExtensions DisableDelayedExpansion

rem In a source checkout, prefer the adjacent bootstrap script so the command
rem can run without a prebuilt native launcher. Packaged payloads carry the
rem same script and use it as the relocatable entry point as well.
set "GUPKG_TUI_SOURCE=%~dp0gupkg\gupkg-tui.cmd"
if exist "%GUPKG_TUI_SOURCE%" if not exist "%GUPKG_TUI_SOURCE%\NUL" goto :source_tui

rem Otherwise select the package-local native command before looking for an installed one.
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

:source_tui
call "%GUPKG_TUI_SOURCE%" %*
exit /b %ERRORLEVEL%
