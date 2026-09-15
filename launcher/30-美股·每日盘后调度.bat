@echo off
rem task key: us_daily
rem Chinese title lives in tools/launcher.py -- this file stays ASCII-only.
call "%~dp0_common.bat" us_daily
exit /b %ERRORLEVEL%
