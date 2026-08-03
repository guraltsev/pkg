@echo off
setlocal EnableExtensions DisableDelayedExpansion

rem Open pkg's interactive terminal interface.
call "%~dp0pkg.cmd" tui %*
exit /b %ERRORLEVEL%
