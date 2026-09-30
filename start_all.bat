@echo off
setlocal
cd /d "%~dp0"
title Three-platform collector - Ctrl+C to stop
set "RUN_ARGS=%*"
if "%~1"=="" set "RUN_ARGS=--fail-fast --retries 0"
where python >nul 2>nul
if errorlevel 1 (
    py -3 start_all.py %RUN_ARGS%
) else (
    python start_all.py %RUN_ARGS%
)
set "RC=%ERRORLEVEL%"
echo.
echo Collector exit code: %RC%
if not "%COLLECTOR_TEST_NO_PAUSE%"=="1" pause
exit /b %RC%
