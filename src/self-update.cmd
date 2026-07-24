@echo off
setlocal EnableExtensions DisableDelayedExpansion

rem Update the bootstrapped pkg installation identified by PKG_HOME.
call "%~dp0pkg.cmd" --action SelfUpdate --pause %*
exit /b %ERRORLEVEL%
