@echo off
setlocal EnableExtensions DisableDelayedExpansion

rem Check, prepare, and install an available package update.
call "%~dp0pkg.cmd" --action Update --pause %*
exit /b %ERRORLEVEL%
