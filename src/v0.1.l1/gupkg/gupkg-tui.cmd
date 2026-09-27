@echo off
setlocal EnableExtensions DisableDelayedExpansion

rem Keep TUI intent in one internal bootstrap path.
call "%~dp0gupkg.cmd" tui %*
exit /b %ERRORLEVEL%
