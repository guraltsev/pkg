@echo off
setlocal EnableExtensions DisableDelayedExpansion

rem Download an available package update. Activate it with `pkg upgrade install`.
call "%~dp0pkg.cmd" --pause upgrade download %*
exit /b %ERRORLEVEL%
