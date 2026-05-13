@echo off
setlocal EnableExtensions DisableDelayedExpansion

rem -----------------------------------------------------------------------------
rem pkg.cmd - bootstrap launcher for pkg.py
rem
rem Interpreter selection priority:
rem   1) Command line: --python <PYTHON_EXE>
rem   2) Environment:  PKG_PYTHON
rem   3) File:         <this script dir>\pkg.python  (first non-empty line)
rem   4) Fallback:     python  (from PATH)
rem
rem This launcher intentionally does NOT parse pkg.toml.
rem If an override source is provided but invalid, it fails loudly.
rem -----------------------------------------------------------------------------

set "SCRIPT_DIR=%~dp0"
set "PKG_PY=%SCRIPT_DIR%pkg.py"
set "PKG_PYTHON_FILE=%SCRIPT_DIR%pkg.python"

if not exist "%PKG_PY%" (
  echo [pkg] ERROR: Missing pkg.py next to pkg.cmd: "%PKG_PY%"
  exit /b 2
)

set "PYTHON_EXE="
set "PYTHON_SOURCE="

rem 1) --python <...> (scan args; pkg.py accepts --python so we can forward %*)
call :ParsePythonArg %*
if errorlevel 1 exit /b %ERRORLEVEL%

rem 2) ENV: PKG_PYTHON
if not defined PYTHON_EXE if defined PKG_PYTHON (
  set "PYTHON_EXE=%PKG_PYTHON%"
  set "PYTHON_SOURCE=PKG_PYTHON"
)

rem 3) File: pkg.python next to this script
if not defined PYTHON_EXE if exist "%PKG_PYTHON_FILE%" (
  call :LoadPythonFromFile "%PKG_PYTHON_FILE%"
  if errorlevel 1 exit /b %ERRORLEVEL%
)

rem 4) Fallback
if not defined PYTHON_EXE (
  set "PYTHON_EXE=python"
  set "PYTHON_SOURCE=PATH"
)

call :NormalizePythonExe
call :ValidatePythonExe
if errorlevel 1 exit /b %ERRORLEVEL%

echo [pkg] Using Python [source=%PYTHON_SOURCE%]: "%PYTHON_EXE%"

"%PYTHON_EXE%" "%PKG_PY%" %*
exit /b %ERRORLEVEL%


:ParsePythonArg
rem Parse --python <exe> or --python=<exe>. Last one wins.
:PPA_LOOP
if "%~1"=="" exit /b 0

if /I "%~1"=="--python" (
  if "%~2"=="" (
    echo [pkg] ERROR: --python requires a value.
    exit /b 2
  )
  set "PYTHON_EXE=%~2"
  set "PYTHON_SOURCE=--python"
  shift
  shift
  goto :PPA_LOOP
)

set "ARG=%~1"
if /I "%ARG:~0,9%"=="--python=" (
  if "%ARG:~9%"=="" (
    echo [pkg] ERROR: --python= requires a value.
    exit /b 2
  )
  set "PYTHON_EXE=%ARG:~9%"
  set "PYTHON_SOURCE=--python"
)

shift
goto :PPA_LOOP


:LoadPythonFromFile
set "FILE=%~1"
for /f "usebackq delims=" %%L in ("%FILE%") do (
  set "PYTHON_EXE=%%L"
  set "PYTHON_SOURCE=pkg.python"
  goto :EOF
)
echo [pkg] ERROR: "%FILE%" exists but contains no Python path.
echo [pkg]        Put a single python.exe path (or a command like python) on the first non-empty line.
exit /b 2


:NormalizePythonExe
rem Trim leading spaces
for /f "tokens=* delims= " %%A in ("%PYTHON_EXE%") do set "PYTHON_EXE=%%A"

rem Strip surrounding quotes if present
if "%PYTHON_EXE:~0,1%"=="\"" if "%PYTHON_EXE:~-1%"=="\"" set "PYTHON_EXE=%PYTHON_EXE:~1,-1%"

rem If value looks like a relative path and exists next to this script, make it absolute.
set "CAND=%PYTHON_EXE%"

set "IS_ABS=0"
if "%CAND:~0,2%"=="\\" set "IS_ABS=1"
if "%CAND:~0,1%"=="\" set "IS_ABS=1"
if "%CAND:~1,1%"==":" set "IS_ABS=1"

set "LOOKS_PATHLIKE=0"
if not "%CAND%"=="%CAND:\=%" set "LOOKS_PATHLIKE=1"
if not "%CAND%"=="%CAND:/=%" set "LOOKS_PATHLIKE=1"
if "%CAND:~0,1%"=="." set "LOOKS_PATHLIKE=1"

if "%IS_ABS%"=="0" if "%LOOKS_PATHLIKE%"=="1" (
  if exist "%SCRIPT_DIR%%CAND%" for %%I in ("%SCRIPT_DIR%%CAND%") do set "PYTHON_EXE=%%~fI"
)
exit /b 0


:ValidatePythonExe
rem Fail loudly when the chosen interpreter is not usable.
set "CAND=%PYTHON_EXE%"

set "IS_PATHLIKE=0"
if not "%CAND%"=="%CAND:\=%" set "IS_PATHLIKE=1"
if not "%CAND%"=="%CAND:/=%" set "IS_PATHLIKE=1"
if not "%CAND%"=="%CAND::=%" set "IS_PATHLIKE=1"
if "%CAND:~0,2%"=="\\" set "IS_PATHLIKE=1"

if "%IS_PATHLIKE%"=="1" (
  if exist "%CAND%\NUL" (
    echo [pkg] ERROR: Python interpreter is a directory: "%CAND%" [source=%PYTHON_SOURCE%]
    exit /b 2
  )
  if exist "%CAND%" exit /b 0
  echo [pkg] ERROR: Python interpreter not found: "%CAND%" [source=%PYTHON_SOURCE%]
  echo [pkg]        Fix: pass --python "C:\\Path\\to\\python.exe" or set PKG_PYTHON, or create "%PKG_PYTHON_FILE%".
  exit /b 2
)

where "%CAND%" >nul 2>nul
if errorlevel 1 (
  echo [pkg] ERROR: Python command not found on PATH: "%CAND%" [source=%PYTHON_SOURCE%]
  echo [pkg]        Fix: install Python or use --python, PKG_PYTHON, or "%PKG_PYTHON_FILE%".
  exit /b 2
)

exit /b 0
