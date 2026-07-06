@echo off
setlocal
set "baseDir=%~dp0"

where py >nul 2>&1
if errorlevel 1 (
    python "%baseDir%shortcuts_to_pkg_toml.py" %*
) else (
    py "%baseDir%shortcuts_to_pkg_toml.py" %*
)

endlocal
