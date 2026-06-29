@echo off
REM 自動実行(タスクスケジューラ)用。画面で止まらず、結果はログに残します。
chcp 65001 >nul
cd /d "%~dp0"
python fred_fetcher.py --run >> fred_morning.log 2>&1
