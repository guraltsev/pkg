@echo off
setlocal EnableExtensions DisableDelayedExpansion

rem Validate package metadata and source configuration without installing.
call "%~dp0pkg.cmd" --action HealthCheck --pause %*
exit /b %ERRORLEVEL%
