@echo off
rem task key: check_env
rem Chinese title lives in tools/launcher.py -- this file stays ASCII-only.
call "%~dp0_common.bat" check_env
exit /b %ERRORLEVEL%
