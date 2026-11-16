"""項目を自由に定義できるデータベースツール（カスタムアプリビルダー）。"""
import csv
import io
import json
import os
import re
import sqlite3
import uuid
from datetime import date, datetime, time, timedelta
from pathlib import Path
from urllib.parse import unquote

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
MAX_FILE_SIZE = 10 * 1024 * 1024  # 添付ファイル1件あたりの上限
MAX_FILES_PER_FIELD = 10

# 項目の型。choice 系は選択肢（options）が必須。
FIELD_TYPES = {
    "text": "文字列（1行）",
    "textarea": "文字列（複数行）",
    "number": "数値",
    "date": "日付",
    "time": "時刻",
    "select": "メニュー",
    "radio": "ラジオボタン",
    "multi": "チェックボックス（複数選択）",
    "check": "チェック（オン/オフ）",
    "url": "URL",
    "file": "添付ファイル",
    "lookup": "他アプリのレコード参照",
}
CHOICE_TYPES = {"select", "radio", "multi"}
# 参照項目で「表示に使う項目」として選べる型
LOOKUP_DISPLAY_TYPES = ["text", "number", "date", "time", "select", "radio", "url"]

TEMPLATES = {
    "customers": {
        "name": "顧客名簿",
        "icon": "📇",
        "description": "取引先の連絡先を管理します",
        "fields": [
            {"label": "会社名", "type": "text", "required": True},
            {"label": "担当者", "type": "text"},
            {"label": "電話番号", "type": "text"},
            {"label": "ランク", "type": "select", "options": ["A", "B", "C"]},
            {"label": "Webサイト", "type": "url"},
            {"label": "メモ", "type": "textarea"},
        ],
    },
    "inquiries": {
        "name": "問い合わせ管理",
        "icon": "📨",
        "description": "問い合わせの受付から完了までを記録します",
        "fields": [
            {"label": "件名", "type": "text", "required": True},
            {"label": "受付日", "type": "date", "required": True},
            {"label": "状況", "type": "radio", "required": True,
             "options": ["未対応", "対応中", "完了"]},
            {"label": "種別", "type": "multi", "options": ["質問", "不具合", "要望"]},
            {"label": "対応工数（時間）", "type": "number"},
            {"label": "内容", "type": "textarea"},
        ],
    },
    "equipment": {
        "name": "備品管理",
        "icon": "📦",
        "description": "備品の購入日・金額・保管場所を管理します",
        "fields": [
            {"label": "品名", "type": "text", "required": True},
            {"label": "分類", "type": "select", "options": ["PC", "周辺機器", "什器", "その他"]},
            {"label": "購入日", "type": "date"},
            {"label": "金額", "type": "number"},
            {"label": "保管場所", "type": "text"},
            {"label": "廃棄済み", "type": "check"},
        ],
    },
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS apps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    icon TEXT NOT NULL DEFAULT '📋',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fields (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id INTEGER NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    label TEXT NOT NULL,
    type TEXT NOT NULL,
    required INTEGER NOT NULL DEFAULT 0,
    options TEXT NOT NULL DEFAULT '[]',
    position INTEGER NOT NULL DEFAULT 0,
    config TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id INTEGER NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    data TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    updated_by TEXT NOT NULL DEFAULT ''
);
-- 添付ファイル。record_id が NULL のものはアップロード済みで未保存の仮ファイル。
CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id INTEGER NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    field_id INTEGER NOT NULL,
    record_id INTEGER,
    name TEXT NOT NULL,
    size INTEGER NOT NULL,
    stored TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
    author TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS followers (
    app_id INTEGER NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    user_name TEXT NOT NULL,
    PRIMARY KEY (app_id, user_name)
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recipient TEXT NOT NULL,
    actor TEXT NOT NULL,
    app_id INTEGER NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    record_id INTEGER,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL,
    read INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_fields_app ON fields(app_id, position);
CREATE INDEX IF NOT EXISTS idx_records_app ON records(app_id);
CREATE INDEX IF NOT EXISTS idx_comments_record ON comments(record_id);
CREATE INDEX IF NOT EXISTS idx_attachments_record ON attachments(record_id);
CREATE INDEX IF NOT EXISTS idx_notifications_recipient ON notifications(recipient, id);
"""
# 旧バージョンの DB に不足している列（テーブル, 列, 定義）
MIGRATIONS = [
    ("fields", "config", "TEXT NOT NULL DEFAULT '{}'"),
    ("records", "created_by", "TEXT NOT NULL DEFAULT ''"),
    ("records", "updated_by", "TEXT NOT NULL DEFAULT ''"),
]

_initialized: set[str] = set()


def db_path() -> Path:
    url = os.environ.get("DATABASE_URL", "sqlite:///./data/app.db")
    return Path(url.removeprefix("sqlite:///"))


def upload_dir() -> Path:
    d = db_path().parent / "uploads"
    d.mkdir(parents=True, exist_ok=True)
    return d


def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if str(path) not in _initialized or not path.stat().st_size:
        conn.executescript(SCHEMA)
        for table, column, ddl in MIGRATIONS:
            cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
            if column not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        conn.commit()
        _initialized.add(str(path))
    return conn


def now() -> str:
    return datetime.now().isoformat(sep=" ", timespec="seconds")


def current_user(request: Request) -> str:
    """画面で設定した表示名（認証ではない）。ヘッダーは URL エンコードで届く。"""
    return unquote(request.headers.get("x-user-name", "")).strip()[:50] or "ゲスト"


# ---------- モデル ----------

class FieldIn(BaseModel):
    id: int | None = None
    label: str = Field(min_length=1, max_length=100)
    type: str
    required: bool = False
    options: list[str] = []
    config: dict = {}


class AppIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    icon: str = Field(default="📋", max_length=8)
    template: str | None = None


class FieldsIn(BaseModel):
    fields: list[FieldIn]


class RecordIn(BaseModel):
    data: dict


class CommentIn(BaseModel):
    body: str = Field(min_length=1, max_length=2000)


class FollowIn(BaseModel):
    follow: bool


# ---------- 共通処理 ----------

def field_row(row) -> dict:
    d = dict(row)
    d["required"] = bool(d["required"])
    d["options"] = json.loads(d["options"])
    d["config"] = json.loads(d["config"])
    return d


def get_app(conn, app_id: int) -> dict:
    row = conn.execute("SELECT * FROM apps WHERE id = ?", (app_id,)).fetchone()
    if not row:
        raise HTTPException(404, "アプリが見つかりません")
    return dict(row)


def app_detail(conn, app_id: int, user: str) -> dict:
    following = conn.execute("SELECT 1 FROM followers WHERE app_id = ? AND user_name = ?",
                             (app_id, user)).fetchone()
    return {**get_app(conn, app_id), "fields": get_fields(conn, app_id),
            "following": bool(following)}


def get_fields(conn, app_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM fields WHERE app_id = ? ORDER BY position, id", (app_id,))
    return [field_row(r) for r in rows]


def get_record(conn, app_id: int, record_id: int):
    row = conn.execute(
        "SELECT r.*, (SELECT COUNT(*) FROM comments c WHERE c.record_id = r.id) "
        "AS comment_count FROM records r WHERE r.id = ? AND r.app_id = ?",
        (record_id, app_id)).fetchone()
    if not row:
        raise HTTPException(404, "レコードが見つかりません")
    return row


def marks(items) -> str:
    return ",".join("?" * len(items))


def ref_labels(conn, f: dict, ids) -> dict[int, str]:
    """参照項目について、参照先レコードID → 表示用の文字列を返す。"""
    cfg, ids = f["config"], list(ids)
    if not ids or "app_id" not in cfg:
        return {}
    rows = conn.execute(
        f"SELECT id, json_extract(data, ?) AS v FROM records "
        f"WHERE app_id = ? AND id IN ({marks(ids)})",
        (f'$."{cfg["field_id"]}"', cfg["app_id"], *ids))
    return {r["id"]: f"No.{r['id']}" if r["v"] in (None, "") else str(r["v"]) for r in rows}


def present(conn, fields: list[dict], rows) -> list[dict]:
    """DB の行を API の形にする。参照項目の表示名は refs にまとめて付ける。"""
    recs = []
    for row in rows:
        d = dict(row)
        d["data"] = json.loads(d["data"])
        d["refs"] = {}
        recs.append(d)
    for f in fields:
        if f["type"] != "lookup":
            continue
        key = str(f["id"])
        labels = ref_labels(conn, f, {r["data"][key] for r in recs if key in r["data"]})
        for r in recs:
            rid = r["data"].get(key)
            if rid is not None:
                # 参照先が削除されている場合は label が None になる
                r["refs"][key] = {"id": rid, "app_id": f["config"].get("app_id"),
                                  "label": labels.get(rid)}
    return recs


def remove_files(conn, where: str, args: list):
    """条件に合う添付ファイルを DB とディスクの両方から削除する。"""
    rows = conn.execute(f"SELECT stored FROM attachments WHERE {where}", args).fetchall()
    conn.execute(f"DELETE FROM attachments WHERE {where}", args)
    for r in rows:
        (upload_dir() / r["stored"]).unlink(missing_ok=True)


def sync_attachments(conn, record_id: int, fields: list[dict], cleaned: dict):
    """保存したレコードに添付を紐付け、レコードから外された添付を削除する。"""
    keep = [x["id"] for f in fields if f["type"] == "file"
            for x in cleaned.get(str(f["id"]), [])]
    if keep:
        conn.execute(f"UPDATE attachments SET record_id = ? WHERE id IN ({marks(keep)})",
                     [record_id, *keep])
        remove_files(conn, f"record_id = ? AND id NOT IN ({marks(keep)})", [record_id, *keep])
    else:
        remove_files(conn, "record_id = ?", [record_id])


def notify(conn, app_id: int, record_id: int | None, actor: str, message: str, extra=()):
    """アプリをフォロー中の人と extra に、操作した本人を除いて通知を作る。"""
    rows = conn.execute("SELECT user_name FROM followers WHERE app_id = ?", (app_id,))
    recipients = ({r["user_name"] for r in rows} | {u for u in extra if u}) - {actor}
    ts = now()
    conn.executemany(
        "INSERT INTO notifications (recipient, actor, app_id, record_id, message, created_at) "
        "VALUES (?,?,?,?,?,?)",
        [(u, actor, app_id, record_id, message, ts) for u in recipients])


def clean_fields(items: list[FieldIn]) -> list[FieldIn]:
    """項目定義を検証する。ラベルは CSV の列名になるため重複不可。"""
    seen = set()
    for f in items:
        f.label = f.label.strip()
        if not f.label:
            raise HTTPException(400, "項目名を入力してください")
        if f.label in seen:
            raise HTTPException(400, f"項目名「{f.label}」が重複しています")
        seen.add(f.label)
        if f.type not in FIELD_TYPES:
            raise HTTPException(400, f"不明な項目タイプです: {f.type}")
        if f.type in CHOICE_TYPES:
            opts = list(dict.fromkeys(o.strip() for o in f.options if o.strip()))
            if not opts:
                raise HTTPException(400, f"「{f.label}」の選択肢を1つ以上入力してください")
            f.options = opts
        else:
            f.options = []
        if f.type != "lookup":
            f.config = {}
    return items


def check_lookup(conn, f: FieldIn, old: dict | None):
    """参照項目の設定（参照先アプリと表示に使う項目）を検証する。"""
    try:
        target, display_field = int(f.config["app_id"]), int(f.config["field_id"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, f"「{f.label}」の参照先アプリと表示項目を選択してください")
    row = conn.execute("SELECT type FROM fields WHERE id = ? AND app_id = ?",
                       (display_field, target)).fetchone()
    if not row or row["type"] not in LOOKUP_DISPLAY_TYPES:
        raise HTTPException(400, f"「{f.label}」の表示項目が正しくありません")
    if old and old["config"].get("app_id") != target:
        raise HTTPException(400, f"「{f.label}」: 保存済みの項目の参照先アプリは変更できません")
    f.config = {"app_id": target, "field_id": display_field}


ERROR_MESSAGES = {
    "number": "数値で入力してください",
    "date": "日付の形式が正しくありません（例: 2026-01-31）",
    "time": "時刻の形式が正しくありません（例: 09:30）",
    "url": "http:// または https:// で始まる URL を入力してください",
    "file": f"添付ファイルが正しくありません（1項目 {MAX_FILES_PER_FIELD} 件まで）",
    "lookup": "参照先のレコードが見つかりません",
}


def validate_record(conn, app_id: int, fields: list[dict], data: dict,
                    record_id: int | None = None) -> dict:
    """入力値を項目の型に合わせて検証・正規化する。エラー時は 422 を返す。"""
    cleaned, errors = {}, {}
    for f in fields:
        key, t = str(f["id"]), f["type"]
        v = data.get(key)
        if isinstance(v, str):
            v = v.strip()
        if t == "check":
            v = bool(v)
            if f["required"] and not v:
                errors[key] = "チェックが必要です"
            cleaned[key] = v
            continue
        if v in (None, "", []):
            if f["required"]:
                errors[key] = "入力必須です"
            continue
        try:
            if t == "number":
                if isinstance(v, bool):
                    raise ValueError
                n = float(str(v).replace(",", ""))
                if n != n or n in (float("inf"), float("-inf")):
                    raise ValueError
                v = int(n) if n == int(n) else n
            elif t == "date":
                v = date.fromisoformat(str(v).replace("/", "-")).isoformat()
            elif t == "time":
                v = time.fromisoformat(str(v)).strftime("%H:%M")
            elif t in ("select", "radio"):
                if v not in f["options"]:
                    raise ValueError
            elif t == "multi":
                if not isinstance(v, list) or any(x not in f["options"] for x in v):
                    raise ValueError
                v = [o for o in f["options"] if o in v]
            elif t == "url":
                if not re.match(r"^https?://\S+$", str(v)):
                    raise ValueError
            elif t == "file":
                # このアプリ・この項目にアップロードされ、未保存かこのレコードのものだけ許可
                ids = [x.get("id") if isinstance(x, dict) else x
                       for x in (v if isinstance(v, list) else [None])]
                if any(type(i) is not int for i in ids) or len(ids) > MAX_FILES_PER_FIELD:
                    raise ValueError
                found = {r["id"]: dict(r) for r in conn.execute(
                    f"SELECT id, name, size FROM attachments WHERE app_id = ? AND field_id = ? "
                    f"AND (record_id IS NULL OR record_id IS ?) AND id IN ({marks(ids)})",
                    (app_id, f["id"], record_id, *ids))}
                if len(found) != len(set(ids)):
                    raise ValueError
                v = [found[i] for i in dict.fromkeys(ids)]
            elif t == "lookup":
                v = v.get("id") if isinstance(v, dict) else v
                if isinstance(v, (bool, float)):
                    raise ValueError
                v = int(v)
                if not conn.execute("SELECT 1 FROM records WHERE id = ? AND app_id = ?",
                                    (v, f["config"].get("app_id"))).fetchone():
                    raise ValueError
            else:
                v = str(v)
        except (ValueError, TypeError):
            errors[key] = ERROR_MESSAGES.get(t, "選択肢にない値です")
            continue
        cleaned[key] = v
    if errors:
        raise HTTPException(422, {"errors": errors})
    return cleaned


def build_where(app_id: int, fields: list[dict], params) -> tuple[str, list]:
    """検索語（q）と項目ごとの絞り込み（f_<項目ID>）から WHERE 句を作る。"""
    where, args = ["app_id = ?"], [app_id]
    q = (params.get("q") or "").strip()
    if q:
        where.append("EXISTS (SELECT 1 FROM json_tree(records.data) "
                     "WHERE atom LIKE ? ESCAPE '\\')")
        esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        args.append(f"%{esc}%")
    for f in fields:
        v = params.get(f"f_{f['id']}")
        if v in (None, ""):
            continue
        path = f'$."{f["id"]}"'  # 項目IDは整数なので SQL に埋め込んでも安全
        if f["type"] == "multi":
            where.append(f"EXISTS (SELECT 1 FROM json_each(records.data, '{path}') "
                         "WHERE value = ?)")
            args.append(v)
        elif f["type"] == "check":
            where.append(f"COALESCE(json_extract(data, '{path}'), 0) = ?")
            args.append(1 if v == "1" else 0)
        else:
            where.append(f"json_extract(data, '{path}') = ?")
            args.append(v)
    return " AND ".join(where), args


def display(f: dict, rec: dict) -> str:
    """CSV 用の文字列表現。"""
    v = rec["data"].get(str(f["id"]))
    if v is None:
        return ""
    if f["type"] == "multi":
        return "\n".join(v)
    if f["type"] == "file":
        return "\n".join(x["name"] for x in v)
    if f["type"] == "lookup":
        return rec["refs"][str(f["id"])]["label"] or ""
    if f["type"] == "check":
        return "1" if v else "0"
    return str(v)


# ---------- API: アプリと項目 ----------

app = FastAPI(title="カスタムアプリビルダー")


@app.get("/api/meta")
def meta():
    return {
        "field_types": FIELD_TYPES,
        "lookup_display_types": LOOKUP_DISPLAY_TYPES,
        "max_file_size": MAX_FILE_SIZE,
        "templates": {k: {"name": t["name"], "icon": t["icon"],
                          "description": t["description"]}
                      for k, t in TEMPLATES.items()},
    }


@app.get("/api/apps")
def list_apps():
    with connect() as conn:
        rows = conn.execute(
            "SELECT a.*, (SELECT COUNT(*) FROM records r WHERE r.app_id = a.id) "
            "AS record_count FROM apps a ORDER BY a.id")
        return [dict(r) for r in rows]


@app.post("/api/apps", status_code=201)
def create_app(body: AppIn, request: Request):
    user, fields = current_user(request), []
    if body.template:
        if body.template not in TEMPLATES:
            raise HTTPException(400, "不明なテンプレートです")
        fields = TEMPLATES[body.template]["fields"]
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO apps (name, description, icon, created_at) VALUES (?,?,?,?)",
            (body.name.strip(), body.description.strip(), body.icon or "📋", now()))
        for i, f in enumerate(fields):
            conn.execute(
                "INSERT INTO fields (app_id, label, type, required, options, position) "
                "VALUES (?,?,?,?,?,?)",
                (cur.lastrowid, f["label"], f["type"], int(f.get("required", False)),
                 json.dumps(f.get("options", []), ensure_ascii=False), i))
        # 作成者は最初からそのアプリの通知を受け取る
        conn.execute("INSERT INTO followers (app_id, user_name) VALUES (?,?)",
                     (cur.lastrowid, user))
        return app_detail(conn, cur.lastrowid, user)


@app.get("/api/apps/{app_id}")
def read_app(app_id: int, request: Request):
    with connect() as conn:
        return app_detail(conn, app_id, current_user(request))


@app.put("/api/apps/{app_id}")
def update_app(app_id: int, body: AppIn, request: Request):
    with connect() as conn:
        get_app(conn, app_id)
        conn.execute("UPDATE apps SET name = ?, description = ?, icon = ? WHERE id = ?",
                     (body.name.strip(), body.description.strip(), body.icon or "📋", app_id))
        return app_detail(conn, app_id, current_user(request))


@app.delete("/api/apps/{app_id}", status_code=204)
def delete_app(app_id: int):
    with connect() as conn:
        get_app(conn, app_id)
        remove_files(conn, "app_id = ?", [app_id])
        conn.execute("DELETE FROM apps WHERE id = ?", (app_id,))


@app.put("/api/apps/{app_id}/follow")
def set_follow(app_id: int, body: FollowIn, request: Request):
    user = current_user(request)
    with connect() as conn:
        get_app(conn, app_id)
        if body.follow:
            conn.execute("INSERT OR IGNORE INTO followers (app_id, user_name) VALUES (?,?)",
                         (app_id, user))
        else:
            conn.execute("DELETE FROM followers WHERE app_id = ? AND user_name = ?",
                         (app_id, user))
        return {"following": body.follow}


@app.put("/api/apps/{app_id}/fields")
def replace_fields(app_id: int, body: FieldsIn):
    """項目定義を丸ごと置き換える。id の無い項目は新規、リストに無い既存項目は削除。"""
    items = clean_fields(body.fields)
    with connect() as conn:
        get_app(conn, app_id)
        existing = {f["id"]: f for f in get_fields(conn, app_id)}
        keep = set()
        for pos, f in enumerate(items):
            if f.id is not None:
                if f.id not in existing or f.id in keep:
                    raise HTTPException(400, "項目IDが正しくありません")
                if existing[f.id]["type"] != f.type:
                    raise HTTPException(400, f"「{f.label}」: 既存項目のタイプは変更できません")
                keep.add(f.id)
            if f.type == "lookup":
                check_lookup(conn, f, existing.get(f.id))
            opts = json.dumps(f.options, ensure_ascii=False)
            config = json.dumps(f.config)
            if f.id is None:
                conn.execute(
                    "INSERT INTO fields (app_id, label, type, required, options, position, "
                    "config) VALUES (?,?,?,?,?,?,?)",
                    (app_id, f.label, f.type, int(f.required), opts, pos, config))
            else:
                conn.execute(
                    "UPDATE fields SET label = ?, required = ?, options = ?, position = ?, "
                    "config = ? WHERE id = ?",
                    (f.label, int(f.required), opts, pos, config, f.id))
        for fid in existing.keys() - keep:
            remove_files(conn, "field_id = ?", [fid])
            conn.execute("DELETE FROM fields WHERE id = ?", (fid,))
            conn.execute("UPDATE records SET data = json_remove(data, ?) WHERE app_id = ?",
                         (f'$."{fid}"', app_id))
        return get_fields(conn, app_id)


@app.get("/api/apps/{app_id}/fields/{field_id}/candidates")
def lookup_candidates(app_id: int, field_id: int, q: str = ""):
    """参照項目の入力時に、参照先アプリのレコードを検索して候補を返す。"""
    with connect() as conn:
        f = next((f for f in get_fields(conn, app_id)
                  if f["id"] == field_id and f["type"] == "lookup"), None)
        if not f:
            raise HTTPException(404, "参照項目が見つかりません")
        cfg = f["config"]
        esc = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = conn.execute(
            "SELECT id FROM records WHERE app_id = ? "
            "AND COALESCE(CAST(json_extract(data, ?) AS TEXT), '') LIKE ? ESCAPE '\\' "
            "ORDER BY id DESC LIMIT 20",
            (cfg["app_id"], f'$."{cfg["field_id"]}"', f"%{esc}%")).fetchall()
        labels = ref_labels(conn, f, [r["id"] for r in rows])
        return [{"id": r["id"], "label": labels[r["id"]]} for r in rows]


# ---------- API: レコード ----------

@app.get("/api/apps/{app_id}/records")
def list_records(app_id: int, request: Request, sort: str = "id", order: str = "desc",
                 page: int = 1, per_page: int = 20):
    page, per_page = max(page, 1), min(max(per_page, 1), 200)
    with connect() as conn:
        get_app(conn, app_id)
        fields = get_fields(conn, app_id)
        where, args = build_where(app_id, fields, request.query_params)
        sort_sql = "id"
        if sort in ("created_at", "updated_at"):
            sort_sql = sort
        elif sort.isdigit():
            f = next((f for f in fields if f["id"] == int(sort)), None)
            if f:
                expr = f"json_extract(data, '$.\"{f['id']}\"')"
                sort_sql = f"CAST({expr} AS REAL)" if f["type"] == "number" else expr
        direction = "ASC" if order == "asc" else "DESC"
        total = conn.execute(f"SELECT COUNT(*) FROM records WHERE {where}", args).fetchone()[0]
        rows = conn.execute(
            f"SELECT *, (SELECT COUNT(*) FROM comments c WHERE c.record_id = records.id) "
            f"AS comment_count FROM records WHERE {where} "
            f"ORDER BY {sort_sql} {direction}, id DESC LIMIT ? OFFSET ?",
            [*args, per_page, (page - 1) * per_page]).fetchall()
        return {"total": total, "page": page, "per_page": per_page,
                "records": present(conn, fields, rows)}


@app.get("/api/apps/{app_id}/records/{record_id}")
def read_record(app_id: int, record_id: int):
    with connect() as conn:
        row = get_record(conn, app_id, record_id)
        return present(conn, get_fields(conn, app_id), [row])[0]


@app.post("/api/apps/{app_id}/records", status_code=201)
def create_record(app_id: int, body: RecordIn, request: Request):
    user = current_user(request)
    with connect() as conn:
        get_app(conn, app_id)
        fields = get_fields(conn, app_id)
        data = validate_record(conn, app_id, fields, body.data)
        ts = now()
        rid = conn.execute(
            "INSERT INTO records (app_id, data, created_at, updated_at, created_by, updated_by) "
            "VALUES (?,?,?,?,?,?)",
            (app_id, json.dumps(data, ensure_ascii=False), ts, ts, user, user)).lastrowid
        sync_attachments(conn, rid, fields, data)
        notify(conn, app_id, rid, user, f"{user} さんがレコード No.{rid} を登録しました")
        return present(conn, fields, [get_record(conn, app_id, rid)])[0]


@app.put("/api/apps/{app_id}/records/{record_id}")
def update_record(app_id: int, record_id: int, body: RecordIn, request: Request):
    user = current_user(request)
    with connect() as conn:
        old = get_record(conn, app_id, record_id)
        fields = get_fields(conn, app_id)
        data = validate_record(conn, app_id, fields, body.data, record_id)
        conn.execute("UPDATE records SET data = ?, updated_at = ?, updated_by = ? WHERE id = ?",
                     (json.dumps(data, ensure_ascii=False), now(), user, record_id))
        sync_attachments(conn, record_id, fields, data)
        notify(conn, app_id, record_id, user,
               f"{user} さんがレコード No.{record_id} を更新しました", [old["created_by"]])
        return present(conn, fields, [get_record(conn, app_id, record_id)])[0]


@app.delete("/api/apps/{app_id}/records/{record_id}", status_code=204)
def delete_record(app_id: int, record_id: int, request: Request):
    user = current_user(request)
    with connect() as conn:
        old = get_record(conn, app_id, record_id)
        remove_files(conn, "record_id = ?", [record_id])
        conn.execute("DELETE FROM records WHERE id = ?", (record_id,))
        notify(conn, app_id, None, user,
               f"{user} さんがレコード No.{record_id} を削除しました", [old["created_by"]])


# ---------- API: コメント ----------

@app.get("/api/apps/{app_id}/records/{record_id}/comments")
def list_comments(app_id: int, record_id: int):
    with connect() as conn:
        get_record(conn, app_id, record_id)
        rows = conn.execute("SELECT * FROM comments WHERE record_id = ? ORDER BY id",
                            (record_id,))
        return [dict(r) for r in rows]


@app.post("/api/apps/{app_id}/records/{record_id}/comments", status_code=201)
def add_comment(app_id: int, record_id: int, body: CommentIn, request: Request):
    user, text = current_user(request), body.body.strip()
    if not text:
        raise HTTPException(400, "コメントを入力してください")
    with connect() as conn:
        rec = get_record(conn, app_id, record_id)
        # レコードの作成者と、これまでにコメントした人にも知らせる
        others = [r["author"] for r in conn.execute(
            "SELECT DISTINCT author FROM comments WHERE record_id = ?", (record_id,))]
        cid = conn.execute(
            "INSERT INTO comments (record_id, author, body, created_at) VALUES (?,?,?,?)",
            (record_id, user, text, now())).lastrowid
        excerpt = text[:60] + ("…" if len(text) > 60 else "")
        notify(conn, app_id, record_id, user,
               f"{user} さんがレコード No.{record_id} にコメントしました: {excerpt}",
               [rec["created_by"], *others])
        return dict(conn.execute("SELECT * FROM comments WHERE id = ?", (cid,)).fetchone())


@app.delete("/api/apps/{app_id}/records/{record_id}/comments/{comment_id}", status_code=204)
def delete_comment(app_id: int, record_id: int, comment_id: int, request: Request):
    with connect() as conn:
        get_record(conn, app_id, record_id)
        row = conn.execute("SELECT author FROM comments WHERE id = ? AND record_id = ?",
                           (comment_id, record_id)).fetchone()
        if not row:
            raise HTTPException(404, "コメントが見つかりません")
        if row["author"] != current_user(request):
            raise HTTPException(403, "自分のコメントだけ削除できます")
        conn.execute("DELETE FROM comments WHERE id = ?", (comment_id,))


# ---------- API: 添付ファイル ----------

@app.post("/api/apps/{app_id}/fields/{field_id}/files", status_code=201)
async def upload_file(app_id: int, field_id: int, file: UploadFile = File(...)):
    """ファイルを仮アップロードする。レコード保存時にそのレコードへ紐付けられる。"""
    with connect() as conn:
        if not conn.execute("SELECT 1 FROM fields WHERE id = ? AND app_id = ? AND type = 'file'",
                            (field_id, app_id)).fetchone():
            raise HTTPException(404, "添付ファイル項目が見つかりません")
    stored = uuid.uuid4().hex
    path, size = upload_dir() / stored, 0
    try:
        with path.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_FILE_SIZE:
                    raise HTTPException(
                        413, f"ファイルが大きすぎます（上限 {MAX_FILE_SIZE // 1024 // 1024}MB）")
                out.write(chunk)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    name = Path(file.filename or "file").name[:200] or "file"
    with connect() as conn:
        # 保存されないまま1日以上残っている仮ファイルを掃除する
        limit = (datetime.now() - timedelta(days=1)).isoformat(sep=" ", timespec="seconds")
        remove_files(conn, "record_id IS NULL AND created_at < ?", [limit])
        fid = conn.execute(
            "INSERT INTO attachments (app_id, field_id, name, size, stored, created_at) "
            "VALUES (?,?,?,?,?,?)", (app_id, field_id, name, size, stored, now())).lastrowid
    return {"id": fid, "name": name, "size": size}


@app.get("/api/files/{file_id}")
def download_file(file_id: int):
    with connect() as conn:
        row = conn.execute("SELECT * FROM attachments WHERE id = ?", (file_id,)).fetchone()
    if not row or not (upload_dir() / row["stored"]).is_file():
        raise HTTPException(404, "ファイルが見つかりません")
    # ブラウザ内で開かせず必ずダウンロードさせる（HTML 等をアップロードされても実行されない）
    return FileResponse(upload_dir() / row["stored"], filename=row["name"],
                        media_type="application/octet-stream",
                        headers={"X-Content-Type-Options": "nosniff"})


# ---------- API: 通知 ----------

@app.get("/api/notifications")
def list_notifications(request: Request):
    user = current_user(request)
    with connect() as conn:
        unread = conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE recipient = ? AND read = 0",
            (user,)).fetchone()[0]
        rows = conn.execute(
            "SELECT n.*, a.name AS app_name, a.icon AS app_icon FROM notifications n "
            "JOIN apps a ON a.id = n.app_id WHERE n.recipient = ? ORDER BY n.id DESC LIMIT 50",
            (user,))
        return {"unread": unread, "items": [{**dict(r), "read": bool(r["read"])} for r in rows]}


@app.post("/api/notifications/read", status_code=204)
def read_notifications(request: Request):
    with connect() as conn:
        conn.execute("UPDATE notifications SET read = 1 WHERE recipient = ?",
                     (current_user(request),))


# ---------- API: 集計と CSV ----------

@app.get("/api/apps/{app_id}/aggregate")
def aggregate(app_id: int, group: int, sum: int | None = None):
    """group 項目の値ごとに件数（と sum 項目の合計）を集計する。"""
    with connect() as conn:
        get_app(conn, app_id)
        fields = {f["id"]: f for f in get_fields(conn, app_id)}
        if group not in fields or fields[group]["type"] == "file":
            raise HTTPException(400, "集計項目が正しくありません")
        if sum is not None and (sum not in fields or fields[sum]["type"] != "number"):
            raise HTTPException(400, "合計できるのは数値項目のみです")
        gf, result = fields[group], {}
        rows = conn.execute("SELECT * FROM records WHERE app_id = ?", (app_id,)).fetchall()
        for rec in present(conn, [gf], rows):
            v = rec["data"].get(str(group))
            if gf["type"] == "check":
                keys = ["オン" if v else "オフ"]
            elif gf["type"] == "multi":
                keys = v or ["（未入力）"]
            elif gf["type"] == "lookup" and v is not None:
                keys = [rec["refs"][str(group)]["label"] or "（削除済み）"]
            else:
                keys = ["（未入力）" if v in (None, "") else str(v)]
            amount = rec["data"].get(str(sum), 0) if sum is not None else 0
            for k in keys:
                g = result.setdefault(k, {"key": k, "count": 0, "sum": 0})
                g["count"] += 1
                g["sum"] += amount or 0
        order = {o: i for i, o in enumerate(gf["options"])}
        groups = sorted(result.values(),
                        key=lambda g: (order.get(g["key"], len(order)), g["key"]))
        return {"total": len(rows), "groups": groups}


@app.get("/api/apps/{app_id}/export.csv")
def export_csv(app_id: int, request: Request):
    with connect() as conn:
        a = get_app(conn, app_id)
        fields = get_fields(conn, app_id)
        where, args = build_where(app_id, fields, request.query_params)
        rows = conn.execute(f"SELECT * FROM records WHERE {where} ORDER BY id", args).fetchall()
        recs = present(conn, fields, rows)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ID", *[f["label"] for f in fields], "作成日時", "更新日時"])
    for rec in recs:
        w.writerow([rec["id"], *[display(f, rec) for f in fields],
                    rec["created_at"], rec["updated_at"]])
    # Excel で文字化けしないよう BOM 付き UTF-8 で出力する
    return Response(
        "﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="app-{a["id"]}.csv"'})


@app.post("/api/apps/{app_id}/import")
async def import_csv(app_id: int, request: Request, file: UploadFile = File(...)):
    """CSV を取り込む。1行目を項目名として照合し、エラー行はスキップして報告する。"""
    user = current_user(request)
    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = raw.decode("cp932")  # 日本語版 Excel の既定の文字コード
        except UnicodeDecodeError:
            raise HTTPException(400, "文字コードを判別できません（UTF-8 か Shift_JIS で保存してください）")
    rows = list(csv.reader(io.StringIO(text, newline="")))
    if not rows:
        raise HTTPException(400, "CSV が空です")
    header = [h.strip() for h in rows[0]]
    with connect() as conn:
        get_app(conn, app_id)
        fields = get_fields(conn, app_id)
        # 添付ファイルは CSV では取り込めないので列を無視する
        cols = {f["label"]: f for f in fields if f["type"] != "file"}
        if not any(h in cols for h in header):
            raise HTTPException(400, "1行目にアプリの項目名と一致する列がありません")
        imported, errors = 0, []
        for n, row in enumerate(rows[1:], start=2):
            if not any(c.strip() for c in row):
                continue
            data = {}
            for h, cell in zip(header, row):
                f = cols.get(h)
                if not f:
                    continue
                cell = cell.strip()
                if f["type"] == "multi":
                    cell = [x.strip() for x in re.split(r"[\n;]", cell) if x.strip()]
                elif f["type"] == "check":
                    cell = cell.lower() in ("1", "true", "yes", "on", "はい", "○", "✓")
                elif f["type"] == "lookup" and cell:
                    # 表示項目の値が一致する参照先レコードを探す（無ければ検証でエラーになる）
                    hit = conn.execute(
                        "SELECT id FROM records WHERE app_id = ? "
                        "AND CAST(json_extract(data, ?) AS TEXT) = ? ORDER BY id LIMIT 1",
                        (f["config"].get("app_id"), f'$."{f["config"].get("field_id")}"',
                         cell)).fetchone()
                    cell = hit["id"] if hit else -1
                data[str(f["id"])] = cell
            try:
                cleaned = validate_record(conn, app_id, fields, data)
            except HTTPException as e:
                labels = {str(f["id"]): f["label"] for f in fields}
                msg = "、".join(f"{labels[k]}: {m}" for k, m in e.detail["errors"].items())
                errors.append({"line": n, "message": msg})
                continue
            ts = now()
            conn.execute(
                "INSERT INTO records (app_id, data, created_at, updated_at, created_by, "
                "updated_by) VALUES (?,?,?,?,?,?)",
                (app_id, json.dumps(cleaned, ensure_ascii=False), ts, ts, user, user))
            imported += 1
        if imported:
            notify(conn, app_id, None, user, f"{user} さんが CSV から {imported} 件を取り込みました")
        return {"imported": imported, "errors": errors}


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
