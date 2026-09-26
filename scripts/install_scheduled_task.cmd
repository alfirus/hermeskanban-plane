@echo off
rem Register the Plane<->Kanban sync poller in Windows Task Scheduler.
rem Every 5 minutes, as the current user (needs the user's Hermes home + PATH).
rem Self-heal model: each tick is an independent one-shot run - if a pass dies,
rem the NEXT tick re-runs it; there is no long-lived process to supervise.
setlocal
set "TASKNAME=HermesPlaneSync"
set "RUNNER=%~dp0run_sync.cmd"

schtasks /Create /F /TN "%TASKNAME%" /SC MINUTE /MO 5 /TR "\"%RUNNER%\"" /RL LIMITED
if errorlevel 1 (
  echo Failed to create scheduled task "%TASKNAME%".
  exit /b 1
)

echo.
echo Task "%TASKNAME%" registered: runs every 5 minutes.
echo Verify with:   schtasks /Query /TN "%TASKNAME%" /V /FO LIST
echo Test a run:    "%RUNNER%"
echo Logs:          %USERPROFILE%\.hermes\planesync\logs\planesync.log
exit /b 0
