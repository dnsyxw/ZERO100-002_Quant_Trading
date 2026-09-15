@echo off
rem task key: smoke
rem Chinese title lives in tools/launcher.py -- this file stays ASCII-only.
call "%~dp0_common.bat" smoke
exit /b %ERRORLEVEL%
