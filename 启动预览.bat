@echo off
cd /d "%~dp0"
where pythonw >nul 2>nul
if errorlevel 1 (
  echo Python was not found. Install Python 3.10+ and add it to PATH.
  pause
  exit /b 1
)
start "" pythonw "%~dp0tidy_titles.py" serve
exit /b 0
