@echo off
setlocal
rem Double-click this file to install or update FOLIO-Exchange.
rem It runs install.ps1 from the same folder and passes on any options, for example:
rem   install.cmd -SiteName mylibrary -NoStart
rem Run "powershell -File install.ps1 -?" or see README.md for the options.

if not exist "%~dp0install.ps1" (
    echo install.ps1 is missing. It must be in the same folder as install.cmd.
    echo If you opened install.cmd inside a zip file, extract all the files first, then run install.cmd again.
    echo.
    pause
    exit /b 1
)

"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
set "FX_RESULT=%ERRORLEVEL%"
echo.
pause
exit /b %FX_RESULT%
