@echo off
setlocal EnableExtensions DisableDelayedExpansion

rem Open gupkg's interactive terminal interface.
call "%~dp0gupkg.cmd" tui %*
exit /b %ERRORLEVEL%
