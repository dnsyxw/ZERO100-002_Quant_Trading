@echo off
rem task key: backtest
rem Chinese title lives in tools/launcher.py -- this file stays ASCII-only.
call "%~dp0_common.bat" backtest
exit /b %ERRORLEVEL%
