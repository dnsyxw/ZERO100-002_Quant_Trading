@echo off
rem task key: tests
rem Chinese title lives in tools/launcher.py -- this file stays ASCII-only.
call "%~dp0_common.bat" tests
exit /b %ERRORLEVEL%
