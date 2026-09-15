@echo off
rem task key: research_price
rem Chinese title lives in tools/launcher.py -- this file stays ASCII-only.
call "%~dp0_common.bat" research_price
exit /b %ERRORLEVEL%
