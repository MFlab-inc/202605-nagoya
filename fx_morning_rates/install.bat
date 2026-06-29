@echo off
REM 必要な部品(ライブラリ)をインストールする Windows 用ランチャー。最初に1回だけ実行します。
cd /d "%~dp0"
python -m pip install -r requirements.txt
echo.
echo ----- 終わりました。何かキーを押すと閉じます -----
pause
