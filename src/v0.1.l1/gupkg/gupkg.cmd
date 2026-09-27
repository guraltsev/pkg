@echo off
setlocal EnableExtensions DisableDelayedExpansion
for %%I in ("%~f0") do set "GUPKG_SCRIPT_DIR=%%~dpI"
set "GUPKG_PYTHONPATH=%GUPKG_SCRIPT_DIR%.."
if defined PYTHONPATH set "GUPKG_PYTHONPATH=%GUPKG_PYTHONPATH%;%PYTHONPATH%"
set "PYTHONPATH=%GUPKG_PYTHONPATH%"
set "GUPKG_BUNDLED_PYTHON=%GUPKG_SCRIPT_DIR%python\python.exe"
set "GUPKG_PYTHON_FILE=%GUPKG_SCRIPT_DIR%gupkg.python"
set "GUPKG_PYTHON_OPTIONS="
set "GUPKG_EMBEDDED="
set "GUPKG_BUNDLED_RUNTIME="
set "GUPKG_BOOTSTRAP_DIRECTORY=%GUPKG_SCRIPT_DIR%python"
set "GUPKG_BOOTSTRAP_ARCHIVE=%GUPKG_SCRIPT_DIR%python\python-3.12.10-embed-amd64.zip"
set "GUPKG_PYTHON_URL=https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip"
set "GUPKG_PYTHON_SHA256=4ACBED6DD1C744B0376E3B1CF57CE906F9DC9E95E68824584C8099A63025A3C3"

rem cmd.exe /c quoting can leave a leading quote on forwarded arguments when
rem this launcher is started by the native shim. Normalize every argument
rem through %%~1 before handing the final list to Python.
set "GUPKG_FORWARD_ARGS="
:collect_forward_args
if "%~1"=="" goto :forward_args_ready
set "GUPKG_FORWARD_ARGS=%GUPKG_FORWARD_ARGS% "%~1""
shift
goto :collect_forward_args
:forward_args_ready

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

rem Python's Windows installer can expose only the ``py`` launcher rather than
rem adding ``python`` to PATH. Keep its major-version selector separate so the
rem final invocation can still quote the executable name safely.
call py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
if not errorlevel 1 (
    set "GUPKG_PYTHON=py"
    set "GUPKG_PYTHON_OPTIONS=-3"
)
if defined GUPKG_PYTHON goto :run

rem Download and verify the embedded runtime only when no usable interpreter exists.
goto :bootstrap_embedded_python

:run
if defined GUPKG_EMBEDDED goto :run_embedded
"%GUPKG_PYTHON%" %GUPKG_PYTHON_OPTIONS% "%GUPKG_SCRIPT_DIR%bootstrap.py" %GUPKG_FORWARD_ARGS%
set "GUPKG_EXIT_CODE=%ERRORLEVEL%"
exit /b %GUPKG_EXIT_CODE%

:run_embedded
"%GUPKG_PYTHON%" %GUPKG_PYTHON_OPTIONS% "%GUPKG_SCRIPT_DIR%bootstrap.py" --embedded %GUPKG_FORWARD_ARGS%
set "GUPKG_EXIT_CODE=%ERRORLEVEL%"
exit /b %GUPKG_EXIT_CODE%

:bootstrap_embedded_python
rem Extract only after the archive digest matches the runtime being installed.
if exist "%GUPKG_BUNDLED_PYTHON%" goto :run_bundled_python

rem The source checkout does not include the ignored runtime directory. Create
rem it before asking PowerShell to write the downloaded archive into it.
if not exist "%GUPKG_BOOTSTRAP_DIRECTORY%" mkdir "%GUPKG_BOOTSTRAP_DIRECTORY%" >nul 2>nul
if not exist "%GUPKG_BOOTSTRAP_DIRECTORY%" (
    >&2 echo [gupkg] Could not create the embedded Python directory.
    exit /b 1
)
echo [gupkg] Downloading embedded Python 3.12.10...
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference = 'Stop'; $ProgressPreference = 'SilentlyContinue'; Invoke-WebRequest -Uri $env:GUPKG_PYTHON_URL -OutFile $env:GUPKG_BOOTSTRAP_ARCHIVE; if ((Get-FileHash -Algorithm SHA256 -LiteralPath $env:GUPKG_BOOTSTRAP_ARCHIVE).Hash -ne $env:GUPKG_PYTHON_SHA256) { throw 'Downloaded Python archive failed SHA-256 verification.' }; Expand-Archive -LiteralPath $env:GUPKG_BOOTSTRAP_ARCHIVE -DestinationPath $env:GUPKG_BOOTSTRAP_DIRECTORY -Force; Remove-Item -LiteralPath $env:GUPKG_BOOTSTRAP_ARCHIVE -Force"
if errorlevel 1 (
    echo [gupkg] Could not download or verify embedded Python.
    exit /b 1
)

:run_bundled_python
set "GUPKG_PYTHON=%GUPKG_BUNDLED_PYTHON%"
set "GUPKG_EMBEDDED=1"
goto :run
