#!/bin/bash
# 朝(金利)の取得を実行する Mac 用ランチャー。ダブルクリックで動きます。
cd "$(dirname "$0")"
python3 fred_fetcher.py --run
echo ""
echo "----- 終わりました。Enterキーで閉じます -----"
read
