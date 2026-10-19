@echo off
cd /d "%~dp0"
echo Applying plans\latest.json ...
python "%~dp0tidy_titles.py" apply --latest
if errorlevel 1 (
  echo.
  echo Failed. Make sure ZCode is fully quit (including the tray icon), then retry.
  pause
  exit /b 1
)
echo.
choice /c YN /n /m "Open ZCode now? [Y/N] "
if errorlevel 2 exit /b 0
python "%~dp0tidy_titles.py" open-zcode
exit /b 0
