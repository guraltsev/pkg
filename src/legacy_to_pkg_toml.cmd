@echo off
setlocal

REM ==========================================
REM DEFAULT ARGUMENTS
REM These are always passed to the script BEFORE your manual arguments.
REM Example: set "defaultArgs=--config default.json --verbose"
REM ==========================================
set "defaultArgs="

REM ==========================================
REM USER CONFIGURABLE VARIABLES
REM Set these before calling the script to control its behavior.
REM ==========================================
REM RequiresAdmin: Set to 1 to request admin privileges.
set "RequiresAdmin=0"

REM scriptSubdir: Subdirectory under the script's location where target script lives.
set "scriptSubdir=pkg.modules"

REM scriptName: Name of target script (without path). If empty, uses caller's name + extension.
set "scriptName="

REM scriptType: PS1, PY, BAT, CMD, or EXE.
set "scriptType=PY"

REM runFromScriptDir: Set to 1 to change working directory to script's folder before execution.
set "runFromScriptDir=0"

REM displayHelp: Set to 1 to show Get-Help for PS1 scripts before running.
set "displayHelp=0"

REM pauseAtEnd: Set to 1 to pause after execution (useful for double-click runs).
set "pauseAtEnd=0"

REM debugMode: Set to 1 to print resolved paths and runtime state.
set "debugMode=0"

REM ==========================================
REM ADMIN PRIVILEGES CHECK
REM If RequiresAdmin=1, verify that the script runs with admin rights.
REM ==========================================
set "originalDir=%CD%"
set "didPushd=0"

if "%RequiresAdmin%"=="1" (
    net session >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] This script requires administrative privileges.
        echo Please right-click and "Run as Administrator".
        goto :End
    )
)

REM ==========================================
REM RESOLVE TARGET SCRIPT PATHS
REM Determine absolute directory and fully qualified script name.
REM ==========================================
set "baseDir=%~dp0"

if not "%scriptSubdir%"=="" (
    set "targetDir=%baseDir%%scriptSubdir%\"
) else (
    set "targetDir=%baseDir%"
)

REM Build script name if not explicitly provided:
REM Use the caller's base name (%~n0) and append the appropriate extension.
REM Note: %~n0 works for both .bat and .cmd files.
set "baseName=%~n0"

if "%scriptName%"=="" (
    if /I "%scriptType%"=="PS1" set "scriptName=%baseName%.ps1"
    if /I "%scriptType%"=="PY"  set "scriptName=%baseName%.py"
    if /I "%scriptType%"=="BAT" set "scriptName=%baseName%.bat"
    if /I "%scriptType%"=="CMD" set "scriptName=%baseName%.cmd"
    if /I "%scriptType%"=="EXE" set "scriptName=%baseName%.exe"
)

set "scriptFullName=%targetDir%%scriptName%"
for %%I in ("%scriptFullName%") do set "targetScriptDir=%%~dpI"

REM ==========================================
REM VERIFY TARGET SCRIPT EXISTS
REM ==========================================
if not exist "%scriptFullName%" (
    echo [ERROR] Target script not found:
    echo "%scriptFullName%"
    goto :End
)

REM ==========================================
REM OPTIONAL WORKING DIRECTORY CHANGE
REM If runFromScriptDir=1, switch to the folder containing the target script.
REM ==========================================
if "%runFromScriptDir%"=="1" (
    pushd "%targetScriptDir%"
    if errorlevel 1 (
        echo [ERROR] Failed to change working directory:
        echo "%targetScriptDir%"
        goto :End
    )
    set "didPushd=1"
)

if "%debugMode%"=="1" (
    echo [DEBUG] originalDir=%originalDir%
    echo [DEBUG] baseDir=%baseDir%
    echo [DEBUG] targetDir=%targetDir%
    echo [DEBUG] scriptType=%scriptType%
    echo [DEBUG] scriptName=%scriptName%
    echo [DEBUG] scriptFullName=%scriptFullName%
    echo [DEBUG] targetScriptDir=%targetScriptDir%
    echo [DEBUG] runFromScriptDir=%runFromScriptDir%
    echo [DEBUG] currentDir=%CD%
    echo ----------------------------------------------------
)

REM ==========================================
REM OPTIONAL HELP DISPLAY (PowerShell only)
REM Shows Get-Help synopsis and description for a PS1 script.
REM ==========================================
if "%displayHelp%"=="1" (
    if /I "%scriptType%"=="PS1" (
        echo Loading Help...
        powershell -NoProfile -ExecutionPolicy Bypass -Command "$h = Get-Help '%scriptFullName%'; Write-Host 'SYNOPSIS:'; Write-Host $h.synopsis; Write-Host 'DESCRIPTION:'; Write-Host $h.description"
        echo ----------------------------------------------------
    )
)

REM ==========================================
REM EXECUTION LOGGING
REM Print what will be run and with which arguments.
REM ==========================================
echo Running: %scriptName%
echo Defaults: %defaultArgs%
echo UserArgs: %*
if "%debugMode%"=="1" echo WorkingDir: %CD%
echo ----------------------------------------------------

REM ==========================================
REM INVOKE THE TARGET SCRIPT
REM Dispatch based on script type. Preserve exclamation marks in arguments
REM because delayed expansion is disabled (no !var! expansion here).
REM ==========================================
if /I "%scriptType%"=="PS1" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%scriptFullName%" %defaultArgs% %*
) else if /I "%scriptType%"=="PY" (
    where py >nul 2>&1
    if errorlevel 1 (
        python "%scriptFullName%" %defaultArgs% %*
    ) else (
        py "%scriptFullName%" %defaultArgs% %*
    )
) else if /I "%scriptType%"=="BAT" (
    call "%scriptFullName%" %defaultArgs% %*
) else if /I "%scriptType%"=="CMD" (
    call "%scriptFullName%" %defaultArgs% %*
) else if /I "%scriptType%"=="EXE" (
    "%scriptFullName%" %defaultArgs% %*
) else (
    echo [ERROR] Script type %scriptType% not implemented.
)

:End
REM ==========================================
REM CLEANUP AND PAUSE
REM Restore original working directory if changed, optionally wait for key.
REM ==========================================
if "%pauseAtEnd%"=="1" (
    pause
)
if "%didPushd%"=="1" popd

endlocal
