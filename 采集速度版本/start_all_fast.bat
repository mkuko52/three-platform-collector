@echo off
call "%~dp0..\start_all.bat" --fail-fast --retries 0 --fast %*
exit /b %ERRORLEVEL%
