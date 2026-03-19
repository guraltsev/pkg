@echo off
setlocal EnableExtensions DisableDelayedExpansion

rem Preserve the caller's working directory so pkg.py can default to "."
rem relative to the package directory from which this wrapper was launched.
call pkg.cmd --pause %*
exit /b %ERRORLEVEL%
