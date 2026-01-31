@echo off
echo Running gu-opt-pkg with Machine scope (requires admin)...
echo.

REM Get the directory where this script is located
set SCRIPT_DIR=%~dp0

REM Change to the script directory
cd /d "%SCRIPT_DIR%"

REM Run the Python script with Machine scope
python pkg.py --scope Machine --pause %* 

echo.