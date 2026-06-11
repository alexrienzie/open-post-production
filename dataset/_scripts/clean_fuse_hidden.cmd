@echo off
REM ----------------------------------------------------------------------
REM Delete bindfs/FUSE shadow files (.fuse_hidden*) from indexes/.
REM
REM These accumulate when a process opens a SQLite file in WAL mode and
REM exits without a clean close — bindfs creates a shadow of the -shm
REM file to keep the inode alive for any lingering handle, and the shadow
REM never gets reaped. Each is exactly 32 KB.
REM
REM Safe to run any time. Idempotent. Reads no other files.
REM
REM Usage:
REM    _scripts\clean_fuse_hidden.cmd
REM
REM Or it runs automatically as STEP 0 of rebuild_all.cmd.
REM ----------------------------------------------------------------------

setlocal
cd /d "%~dp0\.."

set IDX=..\indexes
if not exist "%IDX%" (
    echo [clean_fuse_hidden] indexes folder not found: %IDX%
    exit /b 1
)

REM Count before
for /f %%c in ('dir /a /b "%IDX%\.fuse_hidden*" 2^>nul ^| find /c /v ""') do set BEFORE=%%c
del /F /Q "%IDX%\.fuse_hidden*" 2>nul
for /f %%c in ('dir /a /b "%IDX%\.fuse_hidden*" 2^>nul ^| find /c /v ""') do set AFTER=%%c

if "%BEFORE%"=="0" (
    echo [clean_fuse_hidden] none found.
) else (
    echo [clean_fuse_hidden] removed %BEFORE% shadow file^(s^), %AFTER% remaining.
)

endlocal
