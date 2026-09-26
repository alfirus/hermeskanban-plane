@echo off
rem Run one planesync pass. Used by the scheduled task; safe to run by hand.
setlocal
set "REPO=%~dp0.."
set "PYTHONPATH=%REPO%\src"
python -m planesync --once %*
exit /b %ERRORLEVEL%
