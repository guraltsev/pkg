@echo off
setlocal
pushd "%~dp0"
py ".gitconfig\gitconfig.py" tui %*
set "exit_code=%ERRORLEVEL%"
popd
exit /b %exit_code%
