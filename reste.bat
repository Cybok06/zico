@echo off
cd /d "%~dp0"
echo Running AZICO testing reset...
where py >nul 2>nul
if %ERRORLEVEL% EQU 0 (
  py reste.py
) else (
  python reste.py
)
echo.
pause
