@echo off
rem task key: mcp_selftest
rem Chinese title lives in tools/launcher.py -- this file stays ASCII-only.
call "%~dp0_common.bat" mcp_selftest
exit /b %ERRORLEVEL%
