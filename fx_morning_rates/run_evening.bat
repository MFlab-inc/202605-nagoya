@echo off
chcp 65001 >nul
cd /d "%~dp0"
python fx_fetcher.py --run
echo.
echo ----- Done. Press any key to close. -----
pause
