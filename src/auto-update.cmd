@echo off
setlocal EnableExtensions DisableDelayedExpansion

rem Run the due-check and automatic-activation update policy.
call "%~dp0pkg.cmd" --action AutoUpdate --pause %*
exit /b %ERRORLEVEL%
