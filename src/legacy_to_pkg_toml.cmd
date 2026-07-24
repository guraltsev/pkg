@echo off
setlocal EnableExtensions DisableDelayedExpansion

rem Preserve the caller's working directory and route legacy conversion
rem through the supported pkg action.
call "%~dp0pkg.cmd" --action ConvertLegacy %*
exit /b %ERRORLEVEL%
