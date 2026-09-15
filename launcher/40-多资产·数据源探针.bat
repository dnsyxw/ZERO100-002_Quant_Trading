@echo off
rem task key: gl_probe
rem Chinese title lives in tools/launcher.py -- this file stays ASCII-only.
call "%~dp0_common.bat" gl_probe
exit /b %ERRORLEVEL%
