@echo off
rem task key: portfolio
rem Chinese title lives in tools/launcher.py -- this file stays ASCII-only.
call "%~dp0_common.bat" portfolio
exit /b %ERRORLEVEL%
