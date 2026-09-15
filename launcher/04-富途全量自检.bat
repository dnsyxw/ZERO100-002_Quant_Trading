@echo off
rem task key: live_check
rem Chinese title lives in tools/launcher.py -- this file stays ASCII-only.
call "%~dp0_common.bat" live_check
exit /b %ERRORLEVEL%
