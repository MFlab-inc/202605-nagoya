@echo off
REM 朝(金利)の取得を実行する Windows 用ランチャー。ダブルクリックで動きます。
cd /d "%~dp0"
python fred_fetcher.py --run
echo.
echo ----- 終わりました。何かキーを押すと閉じます -----
pause
