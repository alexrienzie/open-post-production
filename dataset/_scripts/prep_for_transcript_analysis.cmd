@echo off
REM ----------------------------------------------------------------------
REM Pre-flight for the transcript analysis pass (Gemini 2.5 Pro).
REM Run this BEFORE kicking off the actual LLM batch run.
REM
REM   1. Build _prompts/transcript_analysis_prompt.md (controlled vocab)
REM   2. Reserve analysis/craft/relations/embedding_anchor slots on every
REM      transcript record (v3 → v4)
REM   3. Confirm validator imports cleanly
REM   4. Smoke-check GEMINI_API_KEY is set in the current shell
REM ----------------------------------------------------------------------

setlocal
cd /d "%~dp0\.."

echo.
echo ===== STEP 1/4: build transcript prompt context =====
python "_scripts\transcripts\build_transcript_prompt_context.py"
if errorlevel 1 (
    echo Failed to build prompt context.
    pause
    exit /b 1
)

echo.
echo ===== STEP 2/4: reserve transcript schema slots =====
python "_scripts\transcripts\reserve_transcript_slots.py"
if errorlevel 1 (
    echo Slot reservation failed.
    pause
    exit /b 2
)

echo.
echo ===== STEP 3/4: validate validation module =====
python -c "import sys; sys.path.insert(0, '_scripts/transcripts'); sys.path.insert(0, '_scripts'); from validate_transcript_analysis import Validator; v = Validator.from_workspace(); print(f'OK - loaded people={len(v.people_ids)} orgs={len(v.org_ids)} beats={len(v.beat_ids)} places={len(v.place_ids)}')"
if errorlevel 1 (
    echo Validator failed to import.
    pause
    exit /b 3
)

echo.
echo ===== STEP 4/4: GEMINI_API_KEY smoke check =====
if not defined GEMINI_API_KEY (
    echo.
    echo GEMINI_API_KEY is NOT set in this shell.
    echo Set it before running the analysis pass:
    echo    PowerShell:  $env:GEMINI_API_KEY = "your-key"
    echo    cmd.exe:     set GEMINI_API_KEY=your-key
    echo.
    pause
    exit /b 4
)
python -c "import google.generativeai as genai; genai.configure(api_key=__import__('os').environ['GEMINI_API_KEY']); print('OK - google.generativeai imports and accepts the key (no API call made)')"
if errorlevel 1 (
    echo Gemini SDK check failed - install with: pip install google-generativeai
    pause
    exit /b 5
)

echo.
echo ===== READY =====
echo.
echo Pre-flight complete. The transcript analysis pass can now run.
echo Inputs ready:
echo   _prompts\transcript_analysis_prompt.md             ^(loaded by the runner^)
echo   assets\catalog\transcripts\*.transcript.json       ^(slots reserved^)
echo   _scripts\transcripts\validate_transcript_analysis.py           ^(import as Validator^)
echo.
echo Kick off the run via PowerShell:
echo   python _scripts\transcripts\run_transcript_analysis_via_gemini.py --max-records 5    ^(smoke test^)
echo   python _scripts\transcripts\run_transcript_analysis_via_gemini.py                     ^(full pass^)
echo.
echo Each run lands in _runs\transcript_analysis_via_gemini_^<timestamp^>\
echo per the conventions in _runs\README.md.
echo.
pause
endlocal
