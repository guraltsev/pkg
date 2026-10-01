@echo off
setlocal EnableExtensions DisableDelayedExpansion

rem Keep explicit TUI intent while letting the dispatcher resolve the package root.
call "%~dp0gupkg.cmd" tui %*
exit /b %ERRORLEVEL%
