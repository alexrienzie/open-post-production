@echo off
REM ----------------------------------------------------------------------
REM Full rebuild — all idempotent migrations, patches, and refreshes.
REM
REM Runs entirely off local data — no SSD or external mounts required.
REM Double-click to run, or invoke from PowerShell / cmd:
REM    _scripts\rebuild_all.cmd
REM
REM Six steps: patch, org dedup, entity backfill, indexes+MANIFEST, editor DB,
REM rolled-up STATS.json (requires MANIFEST from step 4).
REM Each step is idempotent. Safe to re-run any time after schema or data
REM changes. Every JSON write is atomic (write-tmp + os.replace), so a
REM ctrl-C or kill mid-run leaves the original file intact.
REM ----------------------------------------------------------------------

setlocal
cd /d "%~dp0\.."

echo.
echo ===== STEP 0: clean bindfs shadow files from indexes/ =====
call "_scripts\clean_fuse_hidden.cmd"

echo.
    echo Patch failed but continuing — re-run this cmd to retry.
)

echo.
echo ===== STEP 1/5: dedup orgs.json + rewrite cross-refs =====
python "_scripts\registries\dedup_orgs.py"
if errorlevel 1 (
    echo Orgs dedup failed but continuing.
)

echo.
echo ===== STEP 2/5: heuristic entity backfill across ALL domains =====
python "_scripts\registries\backfill_entity_ids.py" --domain all
if errorlevel 1 (
    echo Entity backfill failed but continuing.
)

echo.
echo ===== STEP 3/5: rebuild MANIFEST.json + reverse indexes =====
python "_scripts\build_indexes.py"
if errorlevel 1 (
    echo Index rebuild failed.
    pause
    exit /b 4
)

echo.
echo ===== STEP 4/5: rebuild editorial_catalog.sqlite =====
python "_scripts\build_editor_db.py"
if errorlevel 1 (
    echo Editor DB rebuild failed.
    pause
    exit /b 5
)

echo.
echo ===== STEP 5/5: rebuild STATS.json =====
python "_scripts\build_stats.py"
if errorlevel 1 (
    echo STATS rebuild failed.
    pause
    exit /b 6
)

echo.
echo ===== DONE =====
echo.
echo All five steps succeeded. Workspace is fully rebuilt and consistent.
echo.
pause
