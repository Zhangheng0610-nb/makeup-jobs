@echo off
rem Makeup jobs manual update - double click to run expanded search update (ASCII only)
rem Cooldown: repeated clicks within 30 min of last completion are skipped automatically.
rem Date gate bypassed in manual mode (runs on any day).
set "BASE=%~dp0"
set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%PY%" set "PY=python"
set "PYTHONUTF8=1"
cd /d "%BASE%"
echo ================================================
echo  Makeup jobs update (expanded search)
echo  Starting: %date% %time%
echo  Note: triggers within 30 min after last
echo        update are skipped automatically
echo ================================================
"%PY%" makeup_task.py makeup manual
echo ================================================
echo  Done. Exit code: %errorlevel%
echo  Check result at: https://makeup.zhangheng666.top
echo ================================================
pause
exit /b %errorlevel%
