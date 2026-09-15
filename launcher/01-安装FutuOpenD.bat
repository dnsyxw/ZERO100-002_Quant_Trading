@echo off
rem task key: install_opend
rem Chinese title lives in tools/launcher.py -- this file stays ASCII-only.
call "%~dp0_common.bat" install_opend
exit /b %ERRORLEVEL%
