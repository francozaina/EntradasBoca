@echo off
cd /d "%~dp0"
where python >nul 2>nul
if %errorlevel%==0 (
    python launch_browsers.py
) else (
    py launch_browsers.py
)
pause
