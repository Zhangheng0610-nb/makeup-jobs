@echo off
rem Makeup jobs auto update - scheduled task entry (ASCII only)
set "BASE=%~dp0"
set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%PY%" set "PY=python"
set "PYTHONUTF8=1"
cd /d "%BASE%"
echo [%date% %time%] start >> "%BASE%update.log"
"%PY%" makeup_task.py makeup >> "%BASE%update.log" 2>&1
echo [%date% %time%] end exit=%errorlevel% >> "%BASE%update.log"
exit /b %errorlevel%
