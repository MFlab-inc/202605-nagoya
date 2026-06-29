@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Installing required libraries...
python -m pip install -r requirements.txt
echo.
echo ----- Done. Press any key to close. -----
pause
