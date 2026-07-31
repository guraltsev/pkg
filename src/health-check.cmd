@echo off
setlocal EnableExtensions DisableDelayedExpansion

rem Validate package metadata and source configuration without installing.
call "%~dp0pkg.cmd" --pause config check %*
exit /b %ERRORLEVEL%
