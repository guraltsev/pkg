@echo off
setlocal EnableExtensions DisableDelayedExpansion

rem Check the configured source for an available package update.
call "%~dp0pkg.cmd" --pause upgrade check %*
exit /b %ERRORLEVEL%
