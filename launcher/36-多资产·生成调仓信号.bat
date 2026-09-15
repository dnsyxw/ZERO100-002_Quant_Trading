@echo off
rem task key: gl_signal
rem Chinese title lives in tools/launcher.py -- this file stays ASCII-only.
call "%~dp0_common.bat" gl_signal
exit /b %ERRORLEVEL%
