@echo off
rem task key: download_data
rem Chinese title lives in tools/launcher.py -- this file stays ASCII-only.
call "%~dp0_common.bat" download_data
exit /b %ERRORLEVEL%
