@echo off
REM Quick wrapper around backup_workspace.ps1 — double-clickable.
REM Output: ..\workspace_backup_<yyyy-mm-dd>.tar.gz
REM Override output dir: backup_workspace.cmd D:\Backups
setlocal
cd /d "%~dp0\.."
if "%~1"=="" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "_scripts\backup_workspace.ps1"
) else (
    powershell -NoProfile -ExecutionPolicy Bypass -File "_scripts\backup_workspace.ps1" -OutDir "%~1"
)
pause
endlocal
