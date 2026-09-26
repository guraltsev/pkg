@echo off
setlocal EnableExtensions DisableDelayedExpansion
rem Repository convenience launcher. Installed users should run gupkg directly.
set "GUPKG_BUNDLED_PYTHON=%~dp0python\python.exe"
set "GUPKG_PYTHON_FILE=%~dp0gupkg.python"
set "GUPKG_BOOTSTRAP_DIRECTORY=%~dp0python"
set "GUPKG_BOOTSTRAP_ARCHIVE=%~dp0python\python-3.12.10-embed-amd64.zip"
set "GUPKG_GET_PIP=%~dp0python\get-pip.py"
set "GUPKG_PYTHON_URL=https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip"
set "GUPKG_PYTHON_SHA256=4ACBED6DD1C744B0376E3B1CF57CE906F9DC9E95E68824584C8099A63025A3C3"
set "GUPKG_GET_PIP_URL=https://bootstrap.pypa.io/get-pip.py"

rem An explicit setting and legacy interpreter file retain their established
rem override behavior before considering bundled or system interpreters.
if defined GUPKG_PYTHON goto :run
if exist "%GUPKG_PYTHON_FILE%" set /p GUPKG_PYTHON=<"%GUPKG_PYTHON_FILE%"
if defined GUPKG_PYTHON goto :run

rem Reuse a previously bootstrapped runtime whenever it can execute gupkg.
if not exist "%GUPKG_BUNDLED_PYTHON%" goto :find_system_python
"%GUPKG_BUNDLED_PYTHON%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
if errorlevel 1 goto :find_system_python
set "GUPKG_PYTHON=%GUPKG_BUNDLED_PYTHON%"
call :configure_embedded_python
if errorlevel 1 exit /b %ERRORLEVEL%
call :ensure_embedded_pip
if errorlevel 1 exit /b %ERRORLEVEL%
goto :run

:find_system_python
rem Do not mistake the Windows Store alias or an old interpreter for a usable
rem system Python. If it cannot run this supported version, bootstrap locally.
call python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
if not errorlevel 1 set "GUPKG_PYTHON=python"
if defined GUPKG_PYTHON goto :run

rem No usable Python is available. Download CPython's official embeddable zip,
rem verify its pinned SHA-256, and expand it beside the copied source tree.
call :bootstrap_embedded_python
if errorlevel 1 exit /b %ERRORLEVEL%
set "GUPKG_PYTHON=%GUPKG_BUNDLED_PYTHON%"

:run
pushd "%~dp0"
rem A copied source directory manages the collection beside its own folder.
"%GUPKG_PYTHON%" -m gupkg --root "%~dp0.." %*
set "GUPKG_EXIT_CODE=%ERRORLEVEL%"
popd
exit /b %GUPKG_EXIT_CODE%

:bootstrap_embedded_python
rem Extract only after the archive's digest matches the runtime we intend to run.
if not exist "%GUPKG_BUNDLED_PYTHON%" (
    echo [gupkg] Downloading embedded Python 3.12.10...
    "%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference = 'Stop'; $ProgressPreference = 'SilentlyContinue'; Invoke-WebRequest -Uri $env:GUPKG_PYTHON_URL -OutFile $env:GUPKG_BOOTSTRAP_ARCHIVE; if ((Get-FileHash -Algorithm SHA256 -LiteralPath $env:GUPKG_BOOTSTRAP_ARCHIVE).Hash -ne $env:GUPKG_PYTHON_SHA256) { throw 'Downloaded Python archive failed SHA-256 verification.' }; Expand-Archive -LiteralPath $env:GUPKG_BOOTSTRAP_ARCHIVE -DestinationPath $env:GUPKG_BOOTSTRAP_DIRECTORY -Force; Remove-Item -LiteralPath $env:GUPKG_BOOTSTRAP_ARCHIVE -Force"
    if errorlevel 1 (
        echo [gupkg] Could not download or verify embedded Python.
        exit /b 1
    )
)

rem Restore the Python path and pip configuration after extraction or repair.
call :configure_embedded_python
if errorlevel 1 exit /b %ERRORLEVEL%
call :ensure_embedded_pip
exit /b %ERRORLEVEL%

:ensure_embedded_pip
rem The official embeddable package omits pip, so bootstrap it once into the
rem runtime's private Lib\site-packages before normal pip targets take effect.
"%GUPKG_BUNDLED_PYTHON%" -m pip --version >nul 2>nul
if not errorlevel 1 exit /b 0
echo [gupkg] Installing pip for embedded Python...
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference = 'Stop'; $ProgressPreference = 'SilentlyContinue'; Invoke-WebRequest -Uri $env:GUPKG_GET_PIP_URL -OutFile $env:GUPKG_GET_PIP"
if errorlevel 1 (
    echo [gupkg] Could not download pip's bootstrap installer.
    exit /b 1
)
set "GUPKG_BOOTSTRAPPING_PIP=1"
"%GUPKG_BUNDLED_PYTHON%" "%GUPKG_GET_PIP%" --no-warn-script-location
set "GUPKG_PIP_INSTALL_EXIT_CODE=%ERRORLEVEL%"
set "GUPKG_BOOTSTRAPPING_PIP="
del /q "%GUPKG_GET_PIP%" >nul 2>nul
if not "%GUPKG_PIP_INSTALL_EXIT_CODE%"=="0" (
    echo [gupkg] Could not install pip for embedded Python.
    exit /b 1
)
exit /b 0

:configure_embedded_python
rem Keep the locally downloaded runtime importable without retaining it in Git.
if not exist "%GUPKG_BOOTSTRAP_DIRECTORY%" mkdir "%GUPKG_BOOTSTRAP_DIRECTORY%"
> "%GUPKG_BOOTSTRAP_DIRECTORY%\python312._pth" (
    echo python312.zip
    echo .
    echo ..
    echo Lib/site-packages
    echo.
    echo import site
)
> "%GUPKG_BOOTSTRAP_DIRECTORY%\sitecustomize.py" (
    echo """Configure pip to keep bundled-runtime dependencies in gupkg's user data."""
    echo.
    echo from __future__ import annotations
    echo.
    echo import os
    echo import site
    echo from pathlib import Path
    echo.
    echo if not os.environ.get^("GUPKG_BOOTSTRAPPING_PIP"^):
    echo     local_app_data = os.environ.get^("LOCALAPPDATA"^)
    echo     if local_app_data:
    echo         pip_target = Path^(local_app_data^) / "gupkg" / "embedded" / "site-packages"
    echo     else:
    echo         pip_target = Path.home^(^) / "AppData" / "Local" / "gupkg" / "embedded" / "site-packages"
    echo     site.addsitedir^(str^(pip_target^)^)
    echo     os.environ.setdefault^("PIP_TARGET", str^(pip_target^)^)
)
exit /b 0
