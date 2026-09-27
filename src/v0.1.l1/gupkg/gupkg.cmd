@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "GUPKG_BUNDLED_PYTHON=%~dp0python\python.exe"
set "GUPKG_PYTHON_FILE=%~dp0gupkg.python"
set "GUPKG_BOOTSTRAP_DIRECTORY=%~dp0python"
set "GUPKG_BOOTSTRAP_ARCHIVE=%~dp0python\python-3.12.10-embed-amd64.zip"
set "GUPKG_PYTHON_URL=https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip"
set "GUPKG_PYTHON_SHA256=4ACBED6DD1C744B0376E3B1CF57CE906F9DC9E95E68824584C8099A63025A3C3"

rem Honor explicit interpreter choices before probing the local or system runtime.
if defined GUPKG_PYTHON goto :run
if exist "%GUPKG_PYTHON_FILE%" set /p GUPKG_PYTHON=<"%GUPKG_PYTHON_FILE%"
if defined GUPKG_PYTHON goto :run

rem Reuse a previously bootstrapped runtime only when it can run gupkg.
if not exist "%GUPKG_BUNDLED_PYTHON%" goto :find_system_python
"%GUPKG_BUNDLED_PYTHON%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
if errorlevel 1 goto :find_system_python
set "GUPKG_PYTHON=%GUPKG_BUNDLED_PYTHON%"
set "GUPKG_EMBEDDED=1"
goto :run

:find_system_python
rem Use a supported system interpreter when one is already available.
call python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
if not errorlevel 1 set "GUPKG_PYTHON=python"
if defined GUPKG_PYTHON goto :run

rem Download and verify the embedded runtime only when no usable interpreter exists.
call :bootstrap_embedded_python
if errorlevel 1 exit /b %ERRORLEVEL%
set "GUPKG_PYTHON=%GUPKG_BUNDLED_PYTHON%"
set "GUPKG_EMBEDDED=1"

:run
pushd "%~dp0.."
if defined GUPKG_EMBEDDED goto :run_embedded
"%GUPKG_PYTHON%" "%~dp0bootstrap.py" --root "%~dp0..\.." %*
set "GUPKG_EXIT_CODE=%ERRORLEVEL%"
popd
exit /b %GUPKG_EXIT_CODE%

:run_embedded
"%GUPKG_PYTHON%" "%~dp0bootstrap.py" --embedded --root "%~dp0..\.." %*
set "GUPKG_EXIT_CODE=%ERRORLEVEL%"
popd
exit /b %GUPKG_EXIT_CODE%

:bootstrap_embedded_python
rem Extract only after the archive digest matches the runtime being installed.
if exist "%GUPKG_BUNDLED_PYTHON%" exit /b 0
echo [gupkg] Downloading embedded Python 3.12.10...
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference = 'Stop'; $ProgressPreference = 'SilentlyContinue'; Invoke-WebRequest -Uri $env:GUPKG_PYTHON_URL -OutFile $env:GUPKG_BOOTSTRAP_ARCHIVE; if ((Get-FileHash -Algorithm SHA256 -LiteralPath $env:GUPKG_BOOTSTRAP_ARCHIVE).Hash -ne $env:GUPKG_PYTHON_SHA256) { throw 'Downloaded Python archive failed SHA-256 verification.' }; Expand-Archive -LiteralPath $env:GUPKG_BOOTSTRAP_ARCHIVE -DestinationPath $env:GUPKG_BOOTSTRAP_DIRECTORY -Force; Remove-Item -LiteralPath $env:GUPKG_BOOTSTRAP_ARCHIVE -Force"
if errorlevel 1 (
    echo [gupkg] Could not download or verify embedded Python.
    exit /b 1
)
exit /b 0
