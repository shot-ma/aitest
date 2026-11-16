#!/bin/bash
# クラウド環境のセットアップスクリプト
set -e
if [ -f requirements.txt ]; then
  pip install -r requirements.txt
fi
if [ -f package.json ]; then
  npm install
fi
mkdir -p data
