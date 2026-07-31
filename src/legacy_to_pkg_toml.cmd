@echo off
setlocal EnableExtensions DisableDelayedExpansion

rem Preserve the caller's working directory and route legacy conversion
rem through the supported pkg action.
call "%~dp0pkg.cmd" config from-legacy %*
exit /b %ERRORLEVEL%
