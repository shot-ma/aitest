"""項目を自由に定義できるデータベースツール（カスタムアプリビルダー）。"""
import csv
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import sqlite3
import uuid
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
MAX_FILE_SIZE = 10 * 1024 * 1024  # 添付ファイル1件あたりの上限
MAX_FILES_PER_FIELD = 10
SESSION_DAYS = 14
MAX_LOGIN_FAILURES = 5
# アプリに対する権限の強さ。none < viewer（閲覧）< editor（編集）< manager（管理）
RANK = {"none": 0, "viewer": 1, "editor": 2, "manager": 3}

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
    created_at TEXT NOT NULL,
    default_role TEXT NOT NULL DEFAULT 'editor'  -- 個別指定のないユーザーの権限
);
CREATE TABLE IF NOT EXISTS fields (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id INTEGER NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    label TEXT NOT NULL,
    type TEXT NOT NULL,
    required INTEGER NOT NULL DEFAULT 0,
    options TEXT NOT NULL DEFAULT '[]',
    position INTEGER NOT NULL DEFAULT 0,
    config TEXT NOT NULL DEFAULT '{}',
    access TEXT NOT NULL DEFAULT 'all'  -- all（アプリを見られる全員）/ restricted（指定ユーザーのみ閲覧可）
);
CREATE TABLE IF NOT EXISTS field_members (
    field_id INTEGER NOT NULL REFERENCES fields(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    PRIMARY KEY (field_id, user_id)
);
CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id INTEGER NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    data TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    updated_by TEXT NOT NULL DEFAULT '',
    created_by_id INTEGER
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
    created_at TEXT NOT NULL,
    author_id INTEGER
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',  -- admin（システム管理者）/ user
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS app_members (
    app_id INTEGER NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    PRIMARY KEY (app_id, user_id)
);
CREATE TABLE IF NOT EXISTS app_followers (
    app_id INTEGER NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    PRIMARY KEY (app_id, user_id)
);
CREATE TABLE IF NOT EXISTS user_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    app_id INTEGER NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    record_id INTEGER,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL,
    read INTEGER NOT NULL DEFAULT 0
);
-- ビュー: 一覧またはカレンダーの表示設定に名前を付けて保存したもの。position が先頭のものが既定
CREATE TABLE IF NOT EXISTS views (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id INTEGER NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    type TEXT NOT NULL,                  -- table（一覧）/ calendar
    config TEXT NOT NULL DEFAULT '{}',
    access TEXT NOT NULL DEFAULT 'all',  -- all（アプリを見られる全員）/ restricted（指定ユーザーのみ）
    position INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS view_members (
    view_id INTEGER NOT NULL REFERENCES views(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    PRIMARY KEY (view_id, user_id)
);
-- ログイン導入前の、表示名ベースのフォローと通知は引き継げないので破棄する
DROP TABLE IF EXISTS followers;
DROP TABLE IF EXISTS notifications;
CREATE INDEX IF NOT EXISTS idx_fields_app ON fields(app_id, position);
CREATE INDEX IF NOT EXISTS idx_records_app ON records(app_id);
CREATE INDEX IF NOT EXISTS idx_comments_record ON comments(record_id);
CREATE INDEX IF NOT EXISTS idx_attachments_record ON attachments(record_id);
CREATE INDEX IF NOT EXISTS idx_user_notifications ON user_notifications(user_id, id);
"""
# 旧バージョンの DB に不足している列（テーブル, 列, 定義）
MIGRATIONS = [
    ("fields", "config", "TEXT NOT NULL DEFAULT '{}'"),
    ("fields", "access", "TEXT NOT NULL DEFAULT 'all'"),
    ("records", "created_by", "TEXT NOT NULL DEFAULT ''"),
    ("records", "updated_by", "TEXT NOT NULL DEFAULT ''"),
    ("records", "created_by_id", "INTEGER"),
    ("comments", "author_id", "INTEGER"),
    ("apps", "default_role", "TEXT NOT NULL DEFAULT 'editor'"),
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


# ---------- 認証 ----------

def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1)
    return f"{salt.hex()}:{digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    salt = bytes.fromhex(stored.split(":")[0])
    return hmac.compare_digest(hash_password(password, salt), stored)


DUMMY_HASH = hash_password(secrets.token_hex(8))
_login_failures: dict[str, tuple[int, datetime]] = {}  # ユーザーID → (連続失敗回数, ロック解除時刻)


def sha(token: str) -> str:
    """セッショントークンは DB にはハッシュだけを保存する。"""
    return hashlib.sha256(token.encode()).hexdigest()


def current_user(request: Request) -> dict:
    token = request.cookies.get("session")
    if token:
        with connect() as conn:
            row = conn.execute(
                "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
                "WHERE s.token_hash = ? AND s.expires_at > ? AND u.active = 1",
                (sha(token), now())).fetchone()
        if row:
            return dict(row)
    raise HTTPException(401, "ログインが必要です")


def admin_user(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(403, "システム管理者だけが行える操作です")
    return user


User = Annotated[dict, Depends(current_user)]
Admin = Annotated[dict, Depends(admin_user)]


def public_user(u) -> dict:
    return {k: u[k] for k in ("id", "username", "display_name", "role")}


# ---------- モデル ----------

class FieldIn(BaseModel):
    id: int | None = None
    label: str = Field(min_length=1, max_length=100)
    type: str
    required: bool = False
    options: list[str] = []
    config: dict = {}
    access: str = "all"       # restricted なら user_ids の人とアプリ管理者だけが閲覧できる
    user_ids: list[int] = []


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


class AccountIn(BaseModel):
    username: str
    display_name: str
    password: str


class UserCreateIn(AccountIn):
    role: str = "user"


class UserUpdateIn(BaseModel):
    display_name: str
    role: str
    active: bool
    password: str = ""  # 空なら変更しない


class LoginIn(BaseModel):
    username: str
    password: str


class ProfileIn(BaseModel):
    display_name: str


class PasswordIn(BaseModel):
    current: str
    new: str


class ViewIn(BaseModel):
    name: str
    type: str
    config: dict = {}
    access: str = "all"
    user_ids: list[int] = []
    make_default: bool = False  # True なら先頭に移動する（先頭のビューが全員の既定になる）


class MemberIn(BaseModel):
    user_id: int
    role: str


class AccessIn(BaseModel):
    default_role: str
    members: list[MemberIn]


# ---------- 共通処理 ----------

def field_row(row) -> dict:
    d = dict(row)
    d["required"] = bool(d["required"])
    d["options"] = json.loads(d["options"])
    d["config"] = json.loads(d["config"])
    return d


def app_role(conn, app: dict, user: dict) -> str:
    """ユーザーがそのアプリに対して持つ権限。全員向けの既定と個別指定の強い方になる。"""
    if user["role"] == "admin":
        return "manager"
    row = conn.execute("SELECT role FROM app_members WHERE app_id = ? AND user_id = ?",
                       (app["id"], user["id"])).fetchone()
    return max(app["default_role"], row["role"] if row else "none", key=RANK.get)


def get_app(conn, app_id: int, user: dict, need: str = "viewer") -> dict:
    """アプリを取得し、need 以上の権限があることを確かめる。"""
    row = conn.execute("SELECT * FROM apps WHERE id = ?", (app_id,)).fetchone()
    role = app_role(conn, dict(row), user) if row else "none"
    if role == "none":  # 閲覧できないアプリは存在自体を知らせない
        raise HTTPException(404, "アプリが見つかりません")
    if RANK[role] < RANK[need]:
        raise HTTPException(403, "この操作を行う権限がありません")
    return {**dict(row), "role": role}


def can_view(conn, app_id, user: dict) -> bool:
    row = conn.execute("SELECT * FROM apps WHERE id = ?", (app_id,)).fetchone()
    return bool(row) and app_role(conn, dict(row), user) != "none"


def app_detail(conn, app_id: int, user: dict) -> dict:
    following = conn.execute("SELECT 1 FROM app_followers WHERE app_id = ? AND user_id = ?",
                             (app_id, user["id"])).fetchone()
    a = get_app(conn, app_id, user)
    fields, _ = split_fields(a, user, get_fields(conn, app_id))
    if a["role"] != "manager":  # 誰が閲覧できるかの設定は管理者にだけ返す
        fields = [{k: v for k, v in f.items() if k not in ("access", "user_ids")} for f in fields]
    return {**a, "fields": fields, "views": get_views(conn, a, user, fields),
            "following": bool(following)}


def get_fields(conn, app_id: int) -> list[dict]:
    """アプリの全項目（閲覧権限で絞る前）。user_ids は閲覧を許可されたユーザー。"""
    rows = conn.execute(
        "SELECT * FROM fields WHERE app_id = ? ORDER BY position, id", (app_id,)).fetchall()
    members: dict[int, list[int]] = {}
    for m in conn.execute("SELECT m.field_id, m.user_id FROM field_members m "
                          "JOIN fields f ON f.id = m.field_id WHERE f.app_id = ?", (app_id,)):
        members.setdefault(m["field_id"], []).append(m["user_id"])
    return [{**field_row(r), "user_ids": members.get(r["id"], [])} for r in rows]


def split_fields(app: dict, user: dict, fields: list[dict]) -> tuple[list[dict], list[str]]:
    """項目を「このユーザーが閲覧できるもの」と「できないもののキー」に分ける。

    閲覧できない項目は、値を返さず、検索・絞り込み・並び替え・集計・CSV の対象からも外す。
    """
    if app["role"] == "manager":
        return fields, []
    visible = [f for f in fields if f["access"] == "all" or user["id"] in f["user_ids"]]
    return visible, [str(f["id"]) for f in fields if f not in visible]


def lookup_restricted(conn, cfg: dict, user: dict) -> bool:
    """参照先のアプリか表示項目をこのユーザーが閲覧できないとき True（参照先が無ければ False）。"""
    row = conn.execute("SELECT * FROM apps WHERE id = ?", (cfg.get("app_id"),)).fetchone()
    if not row:
        return False
    role = app_role(conn, dict(row), user)
    if role in ("none", "manager"):
        return role == "none"
    f = conn.execute("SELECT access FROM fields WHERE id = ?", (cfg.get("field_id"),)).fetchone()
    return bool(f) and f["access"] == "restricted" and not conn.execute(
        "SELECT 1 FROM field_members WHERE field_id = ? AND user_id = ?",
        (cfg.get("field_id"), user["id"])).fetchone()


def clean_view_config(vtype: str, config: dict, fields: list[dict], strict: bool) -> dict:
    """ビューの設定を検証・正規化する。strict=False のときは、削除された項目などを黙って取り除く。"""
    by_id = {f["id"]: f for f in fields}

    def field_id(v):
        try:
            v = int(v)
        except (TypeError, ValueError):
            return None
        return v if v in by_id else None

    filters, raw = {}, config.get("filters")
    for k, v in (raw.items() if isinstance(raw, dict) else []):
        f = by_id.get(field_id(k))
        if f and f["type"] in CHOICE_TYPES | {"check"} and isinstance(v, str) and v:
            filters[str(f["id"])] = v
    if vtype == "calendar":
        d, t = field_id(config.get("date_field")), field_id(config.get("title_field"))
        if d is None or by_id[d]["type"] != "date":
            if strict:
                raise HTTPException(400, "カレンダーの基準にする日付項目を選択してください")
            d = None
        if t is not None and by_id[t]["type"] == "file":
            t = None
        return {"date_field": d, "title_field": t, "filters": filters}
    raw = config.get("columns")
    columns = [c for c in dict.fromkeys(field_id(c) for c in (raw if isinstance(raw, list) else []))
               if c is not None]
    if strict and not columns:
        raise HTTPException(400, "表示する項目を1つ以上選択してください")
    sort = str(config.get("sort", "id"))
    if sort not in ("id", "created_at", "updated_at") and field_id(sort) is None:
        sort = "id"
    return {"columns": columns, "sort": sort, "filters": filters,
            "order": "asc" if config.get("order") == "asc" else "desc"}


def get_views(conn, app: dict, user: dict, fields: list[dict]) -> list[dict]:
    """ユーザーが使えるビューを並び順で返す。アプリの管理者には全ビューと公開範囲も返す。"""
    manager, views = app["role"] == "manager", []
    for r in conn.execute("SELECT * FROM views WHERE app_id = ? ORDER BY position, id",
                          (app["id"],)).fetchall():
        members = [m["user_id"] for m in conn.execute(
            "SELECT user_id FROM view_members WHERE view_id = ?", (r["id"],))]
        if not manager and r["access"] == "restricted" and user["id"] not in members:
            continue
        v = {"id": r["id"], "name": r["name"], "type": r["type"], "access": r["access"],
             "config": clean_view_config(r["type"], json.loads(r["config"]), fields, False)}
        if manager:
            v["user_ids"] = members
        views.append(v)
    return views


def save_view(conn, app_id: int, body: ViewIn, view_id: int | None) -> int:
    name = body.name.strip()
    if not 1 <= len(name) <= 50:
        raise HTTPException(400, "ビュー名は1〜50文字で入力してください")
    if body.type not in ("table", "calendar") or body.access not in ("all", "restricted"):
        raise HTTPException(400, "ビューの種類または公開範囲が正しくありません")
    config = json.dumps(clean_view_config(body.type, body.config, get_fields(conn, app_id), True))
    ids = list(dict.fromkeys(body.user_ids)) if body.access == "restricted" else []
    if ids and conn.execute(f"SELECT COUNT(*) FROM users WHERE id IN ({marks(ids)})",
                            ids).fetchone()[0] != len(ids):
        raise HTTPException(400, "存在しないユーザーが含まれています")
    if view_id is None:
        pos = conn.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM views WHERE app_id = ?",
                           (app_id,)).fetchone()[0]
        view_id = conn.execute(
            "INSERT INTO views (app_id, name, type, config, access, position) VALUES (?,?,?,?,?,?)",
            (app_id, name, body.type, config, body.access, pos)).lastrowid
    else:
        old = conn.execute("SELECT type FROM views WHERE id = ? AND app_id = ?",
                           (view_id, app_id)).fetchone()
        if not old:
            raise HTTPException(404, "ビューが見つかりません")
        if old["type"] != body.type:
            raise HTTPException(400, "保存済みのビューの種類は変更できません")
        conn.execute("UPDATE views SET name = ?, config = ?, access = ? WHERE id = ?",
                     (name, config, body.access, view_id))
    if body.make_default:
        conn.execute("UPDATE views SET position = (SELECT MIN(position) - 1 FROM views "
                     "WHERE app_id = ?) WHERE id = ?", (app_id, view_id))
    conn.execute("DELETE FROM view_members WHERE view_id = ?", (view_id,))
    conn.executemany("INSERT INTO view_members (view_id, user_id) VALUES (?,?)",
                     [(view_id, uid) for uid in ids])
    return view_id


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


def present(conn, fields: list[dict], rows, user: dict, hidden=()) -> list[dict]:
    """DB の行を API の形にする。参照項目の表示名は refs にまとめて付ける。"""
    recs = []
    for row in rows:
        d = dict(row)
        d["data"] = json.loads(d["data"])
        for key in hidden:  # 閲覧できない項目の値は返さない
            d["data"].pop(key, None)
        d["refs"] = {}
        recs.append(d)
    for f in fields:
        if f["type"] != "lookup":
            continue
        key = str(f["id"])
        # 参照先のアプリや表示項目を閲覧できない人には、参照先の内容を見せない
        restricted = lookup_restricted(conn, f["config"], user)
        labels = {} if restricted else ref_labels(
            conn, f, {r["data"][key] for r in recs if key in r["data"]})
        for r in recs:
            rid = r["data"].get(key)
            if rid is not None:
                # 参照先が削除されている場合と権限がない場合は label が None になる
                r["refs"][key] = {"id": rid, "app_id": f["config"].get("app_id"),
                                  "label": labels.get(rid), "restricted": restricted}
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


def notify(conn, app: dict, record_id: int | None, actor: dict, message: str, extra=()):
    """アプリをフォロー中の人と extra（ユーザーID）に、操作した本人を除いて通知を作る。"""
    rows = conn.execute("SELECT user_id FROM app_followers WHERE app_id = ?", (app["id"],))
    ids = ({r["user_id"] for r in rows} | {u for u in extra if u}) - {actor["id"]}
    ts = now()
    for uid in ids:
        u = conn.execute("SELECT * FROM users WHERE id = ? AND active = 1", (uid,)).fetchone()
        if u and app_role(conn, app, dict(u)) != "none":  # 閲覧できなくなった人には送らない
            conn.execute(
                "INSERT INTO user_notifications (user_id, app_id, record_id, message, "
                "created_at) VALUES (?,?,?,?,?)", (uid, app["id"], record_id, message, ts))


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
        if f.access not in ("all", "restricted"):
            raise HTTPException(400, f"「{f.label}」の閲覧できる人の設定が正しくありません")
        f.user_ids = list(dict.fromkeys(f.user_ids)) if f.access == "restricted" else []
    return items


def check_lookup(conn, f: FieldIn, old: dict | None, user: dict):
    """参照項目の設定（参照先アプリと表示に使う項目）を検証する。"""
    try:
        target, display_field = int(f.config["app_id"]), int(f.config["field_id"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, f"「{f.label}」の参照先アプリと表示項目を選択してください")
    row = conn.execute("SELECT type FROM fields WHERE id = ? AND app_id = ?",
                       (display_field, target)).fetchone()
    if not row or row["type"] not in LOOKUP_DISPLAY_TYPES or not can_view(conn, target, user):
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


def validate_record(conn, app_id: int, fields: list[dict], data: dict, user: dict,
                    record_id: int | None = None, old: dict | None = None, hidden=()) -> dict:
    """入力値を項目の型に合わせて検証・正規化する。エラー時は 422 を返す。"""
    cleaned, errors = {}, {}
    for f in fields:
        key, t = str(f["id"]), f["type"]
        if key in hidden:
            # 閲覧できない項目は編集もできない。更新時は今の値を保ち、新規登録時は空のままにする
            if old and key in old:
                cleaned[key] = old[key]
            continue
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
                v, target = int(v), f["config"].get("app_id")
                # 参照先を閲覧できない人は、今の値をそのまま残すことだけできる
                if v != (old or {}).get(key) and not can_view(conn, target, user):
                    raise ValueError
                if not conn.execute("SELECT 1 FROM records WHERE id = ? AND app_id = ?",
                                    (v, target)).fetchone():
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


def build_where(app_id: int, fields: list[dict], params, hidden=()) -> tuple[str, list]:
    """検索語（q）と項目ごとの絞り込み（f_<項目ID>）から WHERE 句を作る。

    fields には閲覧できる項目だけを、hidden には閲覧できない項目のキーを渡す。
    """
    where, args = ["app_id = ?"], [app_id]
    q = (params.get("q") or "").strip()
    if q:
        esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        # 閲覧できない項目の値は検索対象から外す（検索結果から中身を推測できないように）
        skip = f" AND e.key NOT IN ({marks(hidden)})" if hidden else ""
        where.append(
            "EXISTS (SELECT 1 FROM json_each(records.data) e WHERE "
            "(e.atom LIKE ? ESCAPE '\\' OR (e.type IN ('array', 'object') AND EXISTS "
            "(SELECT 1 FROM json_tree(e.value) t WHERE t.atom LIKE ? ESCAPE '\\')))" + skip + ")")
        args += [f"%{esc}%", f"%{esc}%", *hidden]
    for f in fields:
        path = f'$."{f["id"]}"'  # 項目IDは整数なので SQL に埋め込んでも安全
        if f["type"] == "date":
            # 日付は範囲でも絞り込める（f_<項目ID>_from / _to。カレンダー表示で使う）
            for suffix, op in (("from", ">="), ("to", "<=")):
                d = params.get(f"f_{f['id']}_{suffix}")
                if d:
                    where.append(f"json_extract(data, '{path}') {op} ?")
                    args.append(d)
        v = params.get(f"f_{f['id']}")
        if v in (None, ""):
            continue
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


# ---------- API: 認証とユーザー管理 ----------

app = FastAPI(title="カスタムアプリビルダー")


@app.middleware("http")
async def check_origin(request: Request, call_next):
    """他サイトからの書き込みリクエスト（CSRF）を拒否する。"""
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        hosts = {request.headers.get("host"), request.headers.get("x-forwarded-host")}
        if origin and urlsplit(origin).netloc not in hosts:
            return JSONResponse({"detail": "不正なリクエスト元です"}, status_code=403)
    return await call_next(request)


def start_session(conn, request: Request, response: Response, user_id: int):
    token = secrets.token_urlsafe(32)
    expires = (datetime.now() + timedelta(days=SESSION_DAYS)).isoformat(sep=" ", timespec="seconds")
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now(),))
    conn.execute("INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?,?,?)",
                 (sha(token), user_id, expires))
    secure = os.environ.get("COOKIE_SECURE") == "1" or request.url.scheme == "https"
    response.set_cookie("session", token, max_age=SESSION_DAYS * 86400, httponly=True,
                        samesite="lax", secure=secure)


def check_account(username: str | None, display_name: str | None, password: str | None):
    """アカウント情報を検証する（None の項目は検証しない）。"""
    if username is not None and not re.fullmatch(r"[a-z0-9._-]{3,50}", username):
        raise HTTPException(400, "ユーザーIDは半角英小文字・数字・._- の3〜50文字で入力してください")
    if display_name is not None and not 1 <= len(display_name) <= 50:
        raise HTTPException(400, "表示名は1〜50文字で入力してください")
    if password is not None and not 8 <= len(password) <= 200:
        raise HTTPException(400, "パスワードは8文字以上で入力してください")


def insert_user(conn, body, role: str) -> int:
    username, display_name = body.username.strip().lower(), body.display_name.strip()
    check_account(username, display_name, body.password)
    if conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
        raise HTTPException(400, f"ユーザーID「{username}」は既に使われています")
    return conn.execute(
        "INSERT INTO users (username, display_name, password_hash, role, created_at) "
        "VALUES (?,?,?,?,?)",
        (username, display_name, hash_password(body.password), role, now())).lastrowid


@app.get("/api/auth/state")
def auth_state(request: Request):
    with connect() as conn:
        setup = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    try:
        user = public_user(current_user(request))
    except HTTPException:
        user = None
    return {"setup_required": setup, "user": user}


@app.post("/api/auth/setup")
def auth_setup(body: AccountIn, request: Request, response: Response):
    """ユーザーが1人もいないときだけ使える。最初の1人をシステム管理者として登録する。"""
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            raise HTTPException(403, "初期設定は完了しています")
        uid = insert_user(conn, body, "admin")
        start_session(conn, request, response, uid)
        return public_user(conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone())


@app.post("/api/auth/login")
def auth_login(body: LoginIn, request: Request, response: Response):
    username = body.username.strip().lower()
    count, until = _login_failures.get(username, (0, datetime.min))
    if count >= MAX_LOGIN_FAILURES and datetime.now() < until:
        raise HTTPException(429, "ログインに続けて失敗しました。1分ほど待ってからやり直してください")
    with connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ? AND active = 1",
                           (username,)).fetchone()
        # ユーザーの有無で応答時間が変わらないよう、存在しなくてもハッシュ計算を行う
        ok = check_password(body.password, row["password_hash"] if row else DUMMY_HASH)
        if not row or not ok:
            if datetime.now() >= until:
                count = 0 if count >= MAX_LOGIN_FAILURES else count
            _login_failures[username] = (count + 1, datetime.now() + timedelta(seconds=60))
            raise HTTPException(401, "ユーザーIDまたはパスワードが違います")
        _login_failures.pop(username, None)
        start_session(conn, request, response, row["id"])
        return public_user(row)


@app.post("/api/auth/logout", status_code=204)
def auth_logout(request: Request, response: Response):
    token = request.cookies.get("session")
    if token:
        with connect() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (sha(token),))
    response.delete_cookie("session")


@app.put("/api/auth/profile")
def update_profile(body: ProfileIn, user: User):
    name = body.display_name.strip()
    check_account(None, name, None)
    with connect() as conn:
        conn.execute("UPDATE users SET display_name = ? WHERE id = ?", (name, user["id"]))
    return public_user({**user, "display_name": name})


@app.put("/api/auth/password", status_code=204)
def change_password(body: PasswordIn, request: Request, user: User):
    if not check_password(body.current, user["password_hash"]):
        raise HTTPException(400, "現在のパスワードが違います")
    check_account(None, None, body.new)
    with connect() as conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                     (hash_password(body.new), user["id"]))
        # 他の端末のログインは無効にする
        conn.execute("DELETE FROM sessions WHERE user_id = ? AND token_hash != ?",
                     (user["id"], sha(request.cookies.get("session", ""))))


@app.get("/api/users/directory")
def user_directory(user: User):
    """権限設定の選択肢用。有効なユーザーの名前だけを返す。"""
    with connect() as conn:
        rows = conn.execute("SELECT id, username, display_name FROM users WHERE active = 1 "
                            "ORDER BY display_name, id")
        return [dict(r) for r in rows]


@app.get("/api/users")
def list_users(admin: Admin):
    with connect() as conn:
        rows = conn.execute("SELECT * FROM users ORDER BY id")
        return [{**public_user(r), "active": bool(r["active"]), "created_at": r["created_at"]}
                for r in rows]


@app.post("/api/users", status_code=201)
def create_user(body: UserCreateIn, admin: Admin):
    if body.role not in ("admin", "user"):
        raise HTTPException(400, "種別が正しくありません")
    with connect() as conn:
        uid = insert_user(conn, body, body.role)
        return public_user(conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone())


@app.put("/api/users/{user_id}")
def update_user(user_id: int, body: UserUpdateIn, admin: Admin):
    name = body.display_name.strip()
    check_account(None, name, body.password or None)
    if body.role not in ("admin", "user"):
        raise HTTPException(400, "種別が正しくありません")
    with connect() as conn:
        if not conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone():
            raise HTTPException(404, "ユーザーが見つかりません")
        if body.role != "admin" or not body.active:
            others = conn.execute("SELECT COUNT(*) FROM users WHERE role = 'admin' "
                                  "AND active = 1 AND id != ?", (user_id,)).fetchone()[0]
            if not others:
                raise HTTPException(400, "有効なシステム管理者が1人もいなくなる変更はできません")
        conn.execute("UPDATE users SET display_name = ?, role = ?, active = ? WHERE id = ?",
                     (name, body.role, int(body.active), user_id))
        if body.password:
            conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                         (hash_password(body.password), user_id))
        if body.password or not body.active:
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return {**public_user(row), "active": bool(row["active"])}


# ---------- API: アプリと項目 ----------

@app.get("/api/meta")
def meta(user: User):
    return {
        "field_types": FIELD_TYPES,
        "lookup_display_types": LOOKUP_DISPLAY_TYPES,
        "max_file_size": MAX_FILE_SIZE,
        "templates": {k: {"name": t["name"], "icon": t["icon"],
                          "description": t["description"]}
                      for k, t in TEMPLATES.items()},
    }


@app.get("/api/apps")
def list_apps(user: User):
    with connect() as conn:
        rows = conn.execute(
            "SELECT a.*, (SELECT COUNT(*) FROM records r WHERE r.app_id = a.id) "
            "AS record_count FROM apps a ORDER BY a.id").fetchall()
        apps = [{**dict(r), "role": app_role(conn, dict(r), user)} for r in rows]
        return [a for a in apps if a["role"] != "none"]


@app.post("/api/apps", status_code=201)
def create_app(body: AppIn, user: User):
    fields = []
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
        # 作成者はそのアプリの管理者になり、最初から通知を受け取る
        conn.execute("INSERT INTO app_members (app_id, user_id, role) VALUES (?,?,'manager')",
                     (cur.lastrowid, user["id"]))
        conn.execute("INSERT INTO app_followers (app_id, user_id) VALUES (?,?)",
                     (cur.lastrowid, user["id"]))
        return app_detail(conn, cur.lastrowid, user)


@app.get("/api/apps/{app_id}")
def read_app(app_id: int, user: User):
    with connect() as conn:
        return app_detail(conn, app_id, user)


@app.put("/api/apps/{app_id}")
def update_app(app_id: int, body: AppIn, user: User):
    with connect() as conn:
        get_app(conn, app_id, user, "manager")
        conn.execute("UPDATE apps SET name = ?, description = ?, icon = ? WHERE id = ?",
                     (body.name.strip(), body.description.strip(), body.icon or "📋", app_id))
        return app_detail(conn, app_id, user)


@app.delete("/api/apps/{app_id}", status_code=204)
def delete_app(app_id: int, user: User):
    with connect() as conn:
        get_app(conn, app_id, user, "manager")
        remove_files(conn, "app_id = ?", [app_id])
        conn.execute("DELETE FROM apps WHERE id = ?", (app_id,))


@app.put("/api/apps/{app_id}/follow")
def set_follow(app_id: int, body: FollowIn, user: User):
    with connect() as conn:
        get_app(conn, app_id, user)
        if body.follow:
            conn.execute("INSERT OR IGNORE INTO app_followers (app_id, user_id) VALUES (?,?)",
                         (app_id, user["id"]))
        else:
            conn.execute("DELETE FROM app_followers WHERE app_id = ? AND user_id = ?",
                         (app_id, user["id"]))
        return {"following": body.follow}


@app.get("/api/apps/{app_id}/access")
def read_access(app_id: int, user: User):
    with connect() as conn:
        a = get_app(conn, app_id, user, "manager")
        rows = conn.execute(
            "SELECT m.user_id, m.role, u.username, u.display_name FROM app_members m "
            "JOIN users u ON u.id = m.user_id WHERE m.app_id = ? ORDER BY u.display_name, u.id",
            (app_id,))
        return {"default_role": a["default_role"], "members": [dict(r) for r in rows]}


@app.put("/api/apps/{app_id}/access")
def update_access(app_id: int, body: AccessIn, user: User):
    """アクセス権を丸ごと置き換える。default_role は個別指定のない全ユーザーに適用される。"""
    if body.default_role not in ("none", "viewer", "editor"):
        raise HTTPException(400, "全ユーザーの権限が正しくありません")
    members = {m.user_id: m.role for m in body.members}
    if any(r not in ("viewer", "editor", "manager") for r in members.values()):
        raise HTTPException(400, "権限の種類が正しくありません")
    with connect() as conn:
        get_app(conn, app_id, user, "manager")
        if user["role"] != "admin" and members.get(user["id"]) != "manager":
            raise HTTPException(400, "自分自身の管理権限は外せません")
        ids = list(members)
        known = conn.execute(f"SELECT COUNT(*) FROM users WHERE id IN ({marks(ids)})",
                             ids).fetchone()[0] if ids else 0
        if known != len(ids):
            raise HTTPException(400, "存在しないユーザーが含まれています")
        conn.execute("UPDATE apps SET default_role = ? WHERE id = ?", (body.default_role, app_id))
        conn.execute("DELETE FROM app_members WHERE app_id = ?", (app_id,))
        conn.executemany("INSERT INTO app_members (app_id, user_id, role) VALUES (?,?,?)",
                         [(app_id, uid, role) for uid, role in members.items()])
    return read_access(app_id, user)


@app.post("/api/apps/{app_id}/views", status_code=201)
def create_view(app_id: int, body: ViewIn, user: User):
    with connect() as conn:
        get_app(conn, app_id, user, "manager")
        return {"id": save_view(conn, app_id, body, None)}


@app.put("/api/apps/{app_id}/views/{view_id}")
def update_view(app_id: int, view_id: int, body: ViewIn, user: User):
    with connect() as conn:
        get_app(conn, app_id, user, "manager")
        return {"id": save_view(conn, app_id, body, view_id)}


@app.delete("/api/apps/{app_id}/views/{view_id}", status_code=204)
def delete_view(app_id: int, view_id: int, user: User):
    with connect() as conn:
        get_app(conn, app_id, user, "manager")
        if not conn.execute("DELETE FROM views WHERE id = ? AND app_id = ?",
                            (view_id, app_id)).rowcount:
            raise HTTPException(404, "ビューが見つかりません")


@app.put("/api/apps/{app_id}/fields")
def replace_fields(app_id: int, body: FieldsIn, user: User):
    """項目定義を丸ごと置き換える。id の無い項目は新規、リストに無い既存項目は削除。"""
    items = clean_fields(body.fields)
    with connect() as conn:
        get_app(conn, app_id, user, "manager")
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
                check_lookup(conn, f, existing.get(f.id), user)
            if f.user_ids and conn.execute(
                    f"SELECT COUNT(*) FROM users WHERE id IN ({marks(f.user_ids)})",
                    f.user_ids).fetchone()[0] != len(f.user_ids):
                raise HTTPException(400, f"「{f.label}」: 存在しないユーザーが含まれています")
            opts = json.dumps(f.options, ensure_ascii=False)
            config = json.dumps(f.config)
            if f.id is None:
                f.id = conn.execute(
                    "INSERT INTO fields (app_id, label, type, required, options, position, "
                    "config, access) VALUES (?,?,?,?,?,?,?,?)",
                    (app_id, f.label, f.type, int(f.required), opts, pos, config,
                     f.access)).lastrowid
            else:
                conn.execute(
                    "UPDATE fields SET label = ?, required = ?, options = ?, position = ?, "
                    "config = ?, access = ? WHERE id = ?",
                    (f.label, int(f.required), opts, pos, config, f.access, f.id))
            conn.execute("DELETE FROM field_members WHERE field_id = ?", (f.id,))
            conn.executemany("INSERT INTO field_members (field_id, user_id) VALUES (?,?)",
                             [(f.id, uid) for uid in f.user_ids])
        for fid in existing.keys() - keep:
            remove_files(conn, "field_id = ?", [fid])
            conn.execute("DELETE FROM fields WHERE id = ?", (fid,))
            conn.execute("UPDATE records SET data = json_remove(data, ?) WHERE app_id = ?",
                         (f'$."{fid}"', app_id))
        return get_fields(conn, app_id)


@app.get("/api/apps/{app_id}/fields/{field_id}/candidates")
def lookup_candidates(app_id: int, field_id: int, user: User, q: str = ""):
    """参照項目の入力時に、参照先アプリのレコードを検索して候補を返す。"""
    with connect() as conn:
        a = get_app(conn, app_id, user)
        f = next((f for f in split_fields(a, user, get_fields(conn, app_id))[0]
                  if f["id"] == field_id and f["type"] == "lookup"), None)
        if not f:
            raise HTTPException(404, "参照項目が見つかりません")
        cfg = f["config"]
        if lookup_restricted(conn, cfg, user):
            raise HTTPException(403, "参照先を閲覧する権限がありません")
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
def list_records(app_id: int, request: Request, user: User, sort: str = "id",
                 order: str = "desc", page: int = 1, per_page: int = 20):
    page, per_page = max(page, 1), min(max(per_page, 1), 500)
    with connect() as conn:
        a = get_app(conn, app_id, user)
        fields, hidden = split_fields(a, user, get_fields(conn, app_id))
        where, args = build_where(app_id, fields, request.query_params, hidden)
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
                "records": present(conn, fields, rows, user, hidden)}


@app.get("/api/apps/{app_id}/records/{record_id}")
def read_record(app_id: int, record_id: int, user: User):
    with connect() as conn:
        a = get_app(conn, app_id, user)
        row = get_record(conn, app_id, record_id)
        fields, hidden = split_fields(a, user, get_fields(conn, app_id))
        return present(conn, fields, [row], user, hidden)[0]


@app.post("/api/apps/{app_id}/records", status_code=201)
def create_record(app_id: int, body: RecordIn, user: User):
    name = user["display_name"]
    with connect() as conn:
        a = get_app(conn, app_id, user, "editor")
        fields = get_fields(conn, app_id)
        visible, hidden = split_fields(a, user, fields)
        data = validate_record(conn, app_id, fields, body.data, user, hidden=hidden)
        ts = now()
        rid = conn.execute(
            "INSERT INTO records (app_id, data, created_at, updated_at, created_by, updated_by, "
            "created_by_id) VALUES (?,?,?,?,?,?,?)",
            (app_id, json.dumps(data, ensure_ascii=False), ts, ts, name, name,
             user["id"])).lastrowid
        sync_attachments(conn, rid, fields, data)
        notify(conn, a, rid, user, f"{name} さんがレコード No.{rid} を登録しました")
        return present(conn, visible, [get_record(conn, app_id, rid)], user, hidden)[0]


@app.put("/api/apps/{app_id}/records/{record_id}")
def update_record(app_id: int, record_id: int, body: RecordIn, user: User):
    name = user["display_name"]
    with connect() as conn:
        a = get_app(conn, app_id, user, "editor")
        old = get_record(conn, app_id, record_id)
        fields = get_fields(conn, app_id)
        visible, hidden = split_fields(a, user, fields)
        data = validate_record(conn, app_id, fields, body.data, user, record_id,
                               json.loads(old["data"]), hidden)
        conn.execute("UPDATE records SET data = ?, updated_at = ?, updated_by = ? WHERE id = ?",
                     (json.dumps(data, ensure_ascii=False), now(), name, record_id))
        sync_attachments(conn, record_id, fields, data)
        notify(conn, a, record_id, user,
               f"{name} さんがレコード No.{record_id} を更新しました", [old["created_by_id"]])
        return present(conn, visible, [get_record(conn, app_id, record_id)], user, hidden)[0]


@app.delete("/api/apps/{app_id}/records/{record_id}", status_code=204)
def delete_record(app_id: int, record_id: int, user: User):
    with connect() as conn:
        a = get_app(conn, app_id, user, "editor")
        old = get_record(conn, app_id, record_id)
        remove_files(conn, "record_id = ?", [record_id])
        conn.execute("DELETE FROM records WHERE id = ?", (record_id,))
        notify(conn, a, None, user,
               f"{user['display_name']} さんがレコード No.{record_id} を削除しました",
               [old["created_by_id"]])


# ---------- API: コメント ----------

@app.get("/api/apps/{app_id}/records/{record_id}/comments")
def list_comments(app_id: int, record_id: int, user: User):
    with connect() as conn:
        get_app(conn, app_id, user)
        get_record(conn, app_id, record_id)
        rows = conn.execute("SELECT * FROM comments WHERE record_id = ? ORDER BY id",
                            (record_id,))
        return [dict(r) for r in rows]


@app.post("/api/apps/{app_id}/records/{record_id}/comments", status_code=201)
def add_comment(app_id: int, record_id: int, body: CommentIn, user: User):
    name, text = user["display_name"], body.body.strip()
    if not text:
        raise HTTPException(400, "コメントを入力してください")
    with connect() as conn:
        a = get_app(conn, app_id, user, "editor")
        rec = get_record(conn, app_id, record_id)
        # レコードの作成者と、これまでにコメントした人にも知らせる
        others = [r["author_id"] for r in conn.execute(
            "SELECT DISTINCT author_id FROM comments WHERE record_id = ?", (record_id,))]
        cid = conn.execute(
            "INSERT INTO comments (record_id, author, author_id, body, created_at) "
            "VALUES (?,?,?,?,?)", (record_id, name, user["id"], text, now())).lastrowid
        excerpt = text[:60] + ("…" if len(text) > 60 else "")
        notify(conn, a, record_id, user,
               f"{name} さんがレコード No.{record_id} にコメントしました: {excerpt}",
               [rec["created_by_id"], *others])
        return dict(conn.execute("SELECT * FROM comments WHERE id = ?", (cid,)).fetchone())


@app.delete("/api/apps/{app_id}/records/{record_id}/comments/{comment_id}", status_code=204)
def delete_comment(app_id: int, record_id: int, comment_id: int, user: User):
    with connect() as conn:
        a = get_app(conn, app_id, user)
        get_record(conn, app_id, record_id)
        row = conn.execute("SELECT author_id FROM comments WHERE id = ? AND record_id = ?",
                           (comment_id, record_id)).fetchone()
        if not row:
            raise HTTPException(404, "コメントが見つかりません")
        if row["author_id"] != user["id"] and a["role"] != "manager":
            raise HTTPException(403, "自分のコメントだけ削除できます")
        conn.execute("DELETE FROM comments WHERE id = ?", (comment_id,))


# ---------- API: 添付ファイル ----------

@app.post("/api/apps/{app_id}/fields/{field_id}/files", status_code=201)
async def upload_file(app_id: int, field_id: int, user: User, file: UploadFile = File(...)):
    """ファイルを仮アップロードする。レコード保存時にそのレコードへ紐付けられる。"""
    with connect() as conn:
        a = get_app(conn, app_id, user, "editor")
        if not any(f["id"] == field_id and f["type"] == "file"
                   for f in split_fields(a, user, get_fields(conn, app_id))[0]):
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
def download_file(file_id: int, user: User):
    with connect() as conn:
        row = conn.execute("SELECT * FROM attachments WHERE id = ?", (file_id,)).fetchone()
        # アプリや項目を閲覧できない場合も、存在を知らせないよう 404 にする
        ok = bool(row) and can_view(conn, row["app_id"], user)
        if ok:
            a = get_app(conn, row["app_id"], user)
            ok = any(f["id"] == row["field_id"]
                     for f in split_fields(a, user, get_fields(conn, a["id"]))[0])
        if not ok or not (upload_dir() / row["stored"]).is_file():
            raise HTTPException(404, "ファイルが見つかりません")
    # ブラウザ内で開かせず必ずダウンロードさせる（HTML 等をアップロードされても実行されない）
    return FileResponse(upload_dir() / row["stored"], filename=row["name"],
                        media_type="application/octet-stream",
                        headers={"X-Content-Type-Options": "nosniff"})


# ---------- API: 通知 ----------

@app.get("/api/notifications")
def list_notifications(user: User):
    with connect() as conn:
        unread = conn.execute(
            "SELECT COUNT(*) FROM user_notifications WHERE user_id = ? AND read = 0",
            (user["id"],)).fetchone()[0]
        rows = conn.execute(
            "SELECT n.*, a.name AS app_name, a.icon AS app_icon FROM user_notifications n "
            "JOIN apps a ON a.id = n.app_id WHERE n.user_id = ? ORDER BY n.id DESC LIMIT 50",
            (user["id"],))
        return {"unread": unread, "items": [{**dict(r), "read": bool(r["read"])} for r in rows]}


@app.post("/api/notifications/read", status_code=204)
def read_notifications(user: User):
    with connect() as conn:
        conn.execute("UPDATE user_notifications SET read = 1 WHERE user_id = ?", (user["id"],))


# ---------- API: 集計と CSV ----------

@app.get("/api/apps/{app_id}/aggregate")
def aggregate(app_id: int, group: int, user: User, sum: int | None = None):
    """group 項目の値ごとに件数（と sum 項目の合計）を集計する。"""
    with connect() as conn:
        a = get_app(conn, app_id, user)
        fields = {f["id"]: f for f in split_fields(a, user, get_fields(conn, app_id))[0]}
        if group not in fields or fields[group]["type"] == "file":
            raise HTTPException(400, "集計項目が正しくありません")
        if sum is not None and (sum not in fields or fields[sum]["type"] != "number"):
            raise HTTPException(400, "合計できるのは数値項目のみです")
        gf, result = fields[group], {}
        rows = conn.execute("SELECT * FROM records WHERE app_id = ?", (app_id,)).fetchall()
        for rec in present(conn, [gf], rows, user):
            v = rec["data"].get(str(group))
            if gf["type"] == "check":
                keys = ["オン" if v else "オフ"]
            elif gf["type"] == "multi":
                keys = v or ["（未入力）"]
            elif gf["type"] == "lookup" and v is not None:
                ref = rec["refs"][str(group)]
                keys = [ref["label"] or ("（権限なし）" if ref["restricted"] else "（削除済み）")]
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
def export_csv(app_id: int, request: Request, user: User):
    with connect() as conn:
        a = get_app(conn, app_id, user)
        fields, hidden = split_fields(a, user, get_fields(conn, app_id))
        where, args = build_where(app_id, fields, request.query_params, hidden)
        rows = conn.execute(f"SELECT * FROM records WHERE {where} ORDER BY id", args).fetchall()
        recs = present(conn, fields, rows, user, hidden)
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
async def import_csv(app_id: int, user: User, file: UploadFile = File(...)):
    """CSV を取り込む。1行目を項目名として照合し、エラー行はスキップして報告する。"""
    name = user["display_name"]
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
        a = get_app(conn, app_id, user, "editor")
        fields = get_fields(conn, app_id)
        visible, hidden = split_fields(a, user, fields)
        # 添付ファイルは CSV では取り込めないので列を無視する。閲覧できない項目の列も無視する
        cols = {f["label"]: f for f in visible if f["type"] != "file"}
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
                    cell = hit["id"] if hit and not lookup_restricted(conn, f["config"], user) else -1
                data[str(f["id"])] = cell
            try:
                cleaned = validate_record(conn, app_id, fields, data, user, hidden=hidden)
            except HTTPException as e:
                labels = {str(f["id"]): f["label"] for f in fields}
                msg = "、".join(f"{labels[k]}: {m}" for k, m in e.detail["errors"].items())
                errors.append({"line": n, "message": msg})
                continue
            ts = now()
            conn.execute(
                "INSERT INTO records (app_id, data, created_at, updated_at, created_by, "
                "updated_by, created_by_id) VALUES (?,?,?,?,?,?,?)",
                (app_id, json.dumps(cleaned, ensure_ascii=False), ts, ts, name, name, user["id"]))
            imported += 1
        if imported:
            notify(conn, a, None, user, f"{name} さんが CSV から {imported} 件を取り込みました")
        return {"imported": imported, "errors": errors}


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
