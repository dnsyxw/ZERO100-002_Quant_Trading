@echo off
rem task key: gl_download
rem Chinese title lives in tools/launcher.py -- this file stays ASCII-only.
call "%~dp0_common.bat" gl_download
exit /b %ERRORLEVEL%
