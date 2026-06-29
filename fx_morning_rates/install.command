#!/bin/bash
# 必要な部品(ライブラリ)をインストールする Mac 用ランチャー。最初に1回だけ実行します。
cd "$(dirname "$0")"
python3 -m pip install -r requirements.txt
echo ""
echo "----- 終わりました。Enterキーで閉じます -----"
read
