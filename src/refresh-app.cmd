@echo off
setlocal EnableExtensions DisableDelayedExpansion

rem Repopulate App from its configured origin and repair installation artifacts.
call "%~dp0pkg.cmd" --refresh-app --pause %*
exit /b %ERRORLEVEL%
