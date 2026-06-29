@echo off
REM 夕方(FX)の取得を実行する Windows 用ランチャー。ダブルクリックで動きます。
cd /d "%~dp0"
python fx_fetcher.py --run
echo.
echo ----- 終わりました。何かキーを押すと閉じます -----
pause
