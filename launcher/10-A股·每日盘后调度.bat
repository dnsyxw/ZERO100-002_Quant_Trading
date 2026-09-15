@echo off
rem task key: daily
rem Chinese title lives in tools/launcher.py -- this file stays ASCII-only.
call "%~dp0_common.bat" daily
exit /b %ERRORLEVEL%
