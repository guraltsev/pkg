@echo off
py -3 "%~dp0configure_nsswitch.py"
exit /b %errorlevel%
