# aitest

サイボウズOfficeのカスタムアプリのような、項目を自由に定義できるデータベースツール。

## 機能
- アプリの作成・編集・削除（空のアプリ、またはテンプレート: 顧客名簿 / 問い合わせ管理 / 備品管理）
- 項目の定義（追加・削除・並び替え・必須指定）
  - 文字列（1行/複数行）、数値、日付、時刻、メニュー、ラジオボタン、チェックボックス（複数選択）、チェック（オン/オフ）、URL
- レコードの登録・編集・削除（項目タイプに応じた入力チェック）
- 一覧のキーワード検索、選択項目での絞り込み、並び替え、ページ送り
- 集計（項目の値ごとの件数、数値項目の合計）
- CSV 書き出し（BOM 付き UTF-8）／取り込み（UTF-8・Shift_JIS、1行目の項目名で照合）

ログインや権限管理はありません（アクセスできる人は誰でも全データを編集できます）。

## 構成
- Python + FastAPI（`app/main.py`）
- SQLite（`data/app.db`、レコードの値は JSON で保存）
- 画面はビルド不要の HTML / CSS / JavaScript（`static/`）

## 起動
```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```
ブラウザで http://localhost:8000 を開きます。API 仕様は http://localhost:8000/docs で確認できます。

## テスト
```bash
python -m pytest
```

## クラウド環境
- セットアップ: `scripts/setup.sh`
- 環境変数: `.env.example` を参照（`DATABASE_URL` で DB ファイルの場所を変更可能）
