@echo off
rem Launches gupkg-bootstrap.ps1 with the same arguments, bypassing the
rem script execution policy for this one run only.
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0gupkg-bootstrap.ps1" %*
exit /b %ERRORLEVEL%
