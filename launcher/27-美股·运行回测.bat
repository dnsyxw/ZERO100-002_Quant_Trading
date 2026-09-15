@echo off
rem task key: us_backtest
rem Chinese title lives in tools/launcher.py -- this file stays ASCII-only.
call "%~dp0_common.bat" us_backtest
exit /b %ERRORLEVEL%
