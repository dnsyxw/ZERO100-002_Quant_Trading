@echo off
rem task key: research_env
rem Chinese title lives in tools/launcher.py -- this file stays ASCII-only.
call "%~dp0_common.bat" research_env
exit /b %ERRORLEVEL%
