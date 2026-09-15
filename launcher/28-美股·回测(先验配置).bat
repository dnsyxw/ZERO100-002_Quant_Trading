@echo off
rem task key: us_backtest_prior
rem Chinese title lives in tools/launcher.py -- this file stays ASCII-only.
call "%~dp0_common.bat" us_backtest_prior
exit /b %ERRORLEVEL%
