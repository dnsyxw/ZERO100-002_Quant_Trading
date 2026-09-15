@echo off
rem task key: gl_scan_quick
rem Chinese title lives in tools/launcher.py -- this file stays ASCII-only.
call "%~dp0_common.bat" gl_scan_quick
exit /b %ERRORLEVEL%
