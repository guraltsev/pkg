@echo off
setlocal EnableExtensions DisableDelayedExpansion

rem Select the package-local native command before looking for an installed one.
set "GUPKG_LOCAL=%~dp0gupkg\gupkg.exe"
if not exist "%GUPKG_LOCAL%" goto :find_system
if exist "%GUPKG_LOCAL%\NUL" goto :find_system
set "GUPKG_LOCAL=" & "%GUPKG_LOCAL%" %*
exit /b %ERRORLEVEL%

:find_system
rem Search PATH entries explicitly, excluding an unrelated executable in cwd.
set "GUPKG_SYSTEM="
if not defined PATH goto :no_system
for %%P in ("%PATH:;=" "%") do if not defined GUPKG_SYSTEM if not "%%~P"=="" if /I not "%%~fP\gupkg.exe"=="%CD%\gupkg.exe" if exist "%%~P\gupkg.exe" if not exist "%%~P\gupkg.exe\NUL" set "GUPKG_SYSTEM=%%~fP\gupkg.exe"
:no_system
if not defined GUPKG_SYSTEM (
    >&2 echo [gupkg] No package-local or PATH gupkg.exe was found.
    exit /b 9009
)

set "GUPKG_SYSTEM=" & "%GUPKG_SYSTEM%" %*
exit /b %ERRORLEVEL%
