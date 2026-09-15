@echo off
rem ============================================================================
rem  Shared launcher logic -- DO NOT double-click this file.
rem
rem  Finds Python 3.10, enters the project root, sets PYTHONPATH=pylibs, then
rem  hands over to tools\launcher.py, which owns every localized message.
rem
rem  Usage (must be called):
rem      call "%~dp0_common.bat" <task-key> [nopause]
rem
rem  WHY THIS FILE IS PURE ASCII AND NEVER CALLS `chcp`:
rem    1. Calling `chcp` in the middle of a .bat makes cmd.exe lose its parser
rem       position for every following line -- even in a pure-ASCII file
rem       (measured). No chcp, no problem.
rem    2. One .bat cannot be encoded correctly for both cp936 and UTF-8.
rem    So .bat stays ASCII-only and all localized text lives in Python, which
rem    picks its encoding from whether stdout is an interactive console.
rem ============================================================================
setlocal EnableExtensions

set "TASK=%~1"
set "PAUSE_AT_END=1"
if /i "%~2"=="nopause" set "PAUSE_AT_END=0"

rem This file lives in <project root>\launcher\, so one level up is the root.
set "PROJ=%~dp0.."
pushd "%PROJ%" 2>nul
if errorlevel 1 (
    echo [ERROR] Cannot enter the project directory: %PROJ%
    goto :finish_fail
)

rem ---------------------------------------------------------------- find Python
set "PY="
set "CAND=%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
if exist "%CAND%" set "PY=%CAND%"
if not defined PY (
    py -3.10 -c "import sys" >nul 2>&1
    if not errorlevel 1 set "PY=py -3.10"
)
if not defined PY (
    python -c "import sys" >nul 2>&1
    if not errorlevel 1 set "PY=python"
)
if not defined PY (
    echo [ERROR] No usable Python interpreter found.
    echo         pylibs\ ships cp310 wheels, so Python 3.10 is required.
    echo         Download: https://www.python.org/downloads/release/python-31011/
    goto :finish_fail
)

rem --------------------------------------------------------------- environment
set "PYTHONPATH=%CD%\pylibs"
if not exist "pylibs" (
    echo [WARN] pylibs\ not found -- dependencies may be missing. See README.md.
    echo.
)

set "LAUNCHER=%CD%\tools\launcher.py"
if not exist "%LAUNCHER%" (
    echo [ERROR] Missing: %LAUNCHER%
    goto :finish_fail
)

rem ------------------------------------------------------------------ run
%PY% "%LAUNCHER%" --task %TASK%
set "RC=%ERRORLEVEL%"

popd
if "%PAUSE_AT_END%"=="1" (
    echo.
    pause
)
endlocal & exit /b %RC%

:finish_fail
popd 2>nul
if "%PAUSE_AT_END%"=="1" (
    echo.
    pause
)
endlocal & exit /b 1
