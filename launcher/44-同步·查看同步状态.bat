@echo off
rem task key: git_status
rem Chinese title lives in tools/launcher.py -- this file stays ASCII-only.
call "%~dp0_common.bat" git_status
exit /b %ERRORLEVEL%
