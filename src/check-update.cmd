@echo off
setlocal EnableExtensions DisableDelayedExpansion

rem Check the configured source for an available package update.
call "%~dp0pkg.cmd" --action CheckUpdate --pause %*
exit /b %ERRORLEVEL%
