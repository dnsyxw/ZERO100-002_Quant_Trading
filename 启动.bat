@echo off
rem lianghua one-click entry point -- double-click this file.
rem The menu itself is rendered by tools\launcher.py (see its TASKS table).
rem Folder name "launcher" stays ASCII so this file needs no non-ASCII bytes.
call "%~dp0launcher\_common.bat" menu
exit /b %ERRORLEVEL%
