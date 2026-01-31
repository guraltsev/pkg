:: Version: 0.10.0
:: Last Updated: 2025-12-18
:: Author: Gennady Uraltsev


@echo off
echo Running pkg with User scope...
echo.

REM Get the directory where this script is located
set SCRIPT_DIR=%~dp0

REM Change to the script directory
cd /d "%SCRIPT_DIR%"

REM Run the Python script with default arguments (User scope)
python pkg.py --pause %*

echo.