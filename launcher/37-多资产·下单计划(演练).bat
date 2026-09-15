@echo off
rem task key: gl_orders_plan
rem Chinese title lives in tools/launcher.py -- this file stays ASCII-only.
call "%~dp0_common.bat" gl_orders_plan
exit /b %ERRORLEVEL%
