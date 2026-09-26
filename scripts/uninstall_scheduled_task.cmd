@echo off
rem Remove the sync poller scheduled task.
schtasks /Delete /F /TN "HermesPlaneSync"
exit /b %ERRORLEVEL%
