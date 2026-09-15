@echo off
rem task key: hk_daily
rem Chinese title lives in tools/launcher.py -- this file stays ASCII-only.
call "%~dp0_common.bat" hk_daily
exit /b %ERRORLEVEL%
