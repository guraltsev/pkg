@echo off
setlocal EnableExtensions DisableDelayedExpansion

rem Prefer the package-local bootstrap, which is also the source-checkout entry point.
set "GUPKG_BOOTSTRAP=%~dp0gupkg\gupkg.cmd"
if exist "%GUPKG_BOOTSTRAP%" goto :run_bootstrap

rem If no bootstrap is present, select the package-local native command.
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

:run_bootstrap
call "%GUPKG_BOOTSTRAP%" %*
exit /b %ERRORLEVEL%
