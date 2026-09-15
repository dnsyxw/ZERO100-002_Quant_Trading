@echo off
rem task key: fetch_docs
rem Chinese title lives in tools/launcher.py -- this file stays ASCII-only.
call "%~dp0_common.bat" fetch_docs
exit /b %ERRORLEVEL%
