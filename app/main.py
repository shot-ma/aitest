"""項目を自由に定義できるデータベースツール（カスタムアプリビルダー）。"""
import csv
import io
import json
import os
import re
import sqlite3
from datetime import date, datetime, time
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

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
}
CHOICE_TYPES = {"select", "radio", "multi"}

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
    position INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id INTEGER NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    data TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fields_app ON fields(app_id, position);
CREATE INDEX IF NOT EXISTS idx_records_app ON records(app_id);
"""

_initialized: set[str] = set()


def connect() -> sqlite3.Connection:
    url = os.environ.get("DATABASE_URL", "sqlite:///./data/app.db")
    path = Path(url.removeprefix("sqlite:///"))
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if str(path) not in _initialized or not path.stat().st_size:
        conn.executescript(SCHEMA)
        _initialized.add(str(path))
    return conn


def now() -> str:
    return datetime.now().isoformat(sep=" ", timespec="seconds")


# ---------- モデル ----------

class FieldIn(BaseModel):
    id: int | None = None
    label: str = Field(min_length=1, max_length=100)
    type: str
    required: bool = False
    options: list[str] = []


class AppIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    icon: str = Field(default="📋", max_length=8)
    template: str | None = None


class FieldsIn(BaseModel):
    fields: list[FieldIn]


class RecordIn(BaseModel):
    data: dict


# ---------- 共通処理 ----------

def field_row(row) -> dict:
    d = dict(row)
    d["required"] = bool(d["required"])
    d["options"] = json.loads(d["options"])
    return d


def get_app(conn, app_id: int) -> dict:
    row = conn.execute("SELECT * FROM apps WHERE id = ?", (app_id,)).fetchone()
    if not row:
        raise HTTPException(404, "アプリが見つかりません")
    return dict(row)


def get_fields(conn, app_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM fields WHERE app_id = ? ORDER BY position, id", (app_id,))
    return [field_row(r) for r in rows]


def record_row(row) -> dict:
    d = dict(row)
    d["data"] = json.loads(d["data"])
    return d


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
    return items


def validate_record(fields: list[dict], data: dict) -> dict:
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
            else:
                v = str(v)
        except (ValueError, TypeError):
            errors[key] = {
                "number": "数値で入力してください",
                "date": "日付の形式が正しくありません（例: 2026-01-31）",
                "time": "時刻の形式が正しくありません（例: 09:30）",
                "url": "http:// または https:// で始まる URL を入力してください",
            }.get(t, "選択肢にない値です")
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


def display(f: dict, v) -> str:
    if v is None:
        return ""
    if f["type"] == "multi":
        return "\n".join(v)
    if f["type"] == "check":
        return "1" if v else "0"
    return str(v)


# ---------- API ----------

app = FastAPI(title="カスタムアプリビルダー")


@app.get("/api/meta")
def meta():
    return {
        "field_types": FIELD_TYPES,
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
def create_app(body: AppIn):
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
        return {**get_app(conn, cur.lastrowid), "fields": get_fields(conn, cur.lastrowid)}


@app.get("/api/apps/{app_id}")
def read_app(app_id: int):
    with connect() as conn:
        return {**get_app(conn, app_id), "fields": get_fields(conn, app_id)}


@app.put("/api/apps/{app_id}")
def update_app(app_id: int, body: AppIn):
    with connect() as conn:
        get_app(conn, app_id)
        conn.execute("UPDATE apps SET name = ?, description = ?, icon = ? WHERE id = ?",
                     (body.name.strip(), body.description.strip(), body.icon or "📋", app_id))
        return {**get_app(conn, app_id), "fields": get_fields(conn, app_id)}


@app.delete("/api/apps/{app_id}", status_code=204)
def delete_app(app_id: int):
    with connect() as conn:
        get_app(conn, app_id)
        conn.execute("DELETE FROM apps WHERE id = ?", (app_id,))


@app.put("/api/apps/{app_id}/fields")
def replace_fields(app_id: int, body: FieldsIn):
    """項目定義を丸ごと置き換える。id の無い項目は新規、リストに無い既存項目は削除。"""
    items = clean_fields(body.fields)
    with connect() as conn:
        get_app(conn, app_id)
        existing = {f["id"]: f for f in get_fields(conn, app_id)}
        keep = set()
        for pos, f in enumerate(items):
            opts = json.dumps(f.options, ensure_ascii=False)
            if f.id is None:
                conn.execute(
                    "INSERT INTO fields (app_id, label, type, required, options, position) "
                    "VALUES (?,?,?,?,?,?)",
                    (app_id, f.label, f.type, int(f.required), opts, pos))
                continue
            if f.id not in existing or f.id in keep:
                raise HTTPException(400, "項目IDが正しくありません")
            if existing[f.id]["type"] != f.type:
                raise HTTPException(400, f"「{f.label}」: 既存項目のタイプは変更できません")
            keep.add(f.id)
            conn.execute(
                "UPDATE fields SET label = ?, required = ?, options = ?, position = ? "
                "WHERE id = ?", (f.label, int(f.required), opts, pos, f.id))
        for fid in existing.keys() - keep:
            conn.execute("DELETE FROM fields WHERE id = ?", (fid,))
            conn.execute("UPDATE records SET data = json_remove(data, ?) WHERE app_id = ?",
                         (f'$."{fid}"', app_id))
        return get_fields(conn, app_id)


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
            f"SELECT * FROM records WHERE {where} ORDER BY {sort_sql} {direction}, id DESC "
            "LIMIT ? OFFSET ?", [*args, per_page, (page - 1) * per_page])
        return {"total": total, "page": page, "per_page": per_page,
                "records": [record_row(r) for r in rows]}


@app.post("/api/apps/{app_id}/records", status_code=201)
def create_record(app_id: int, body: RecordIn):
    with connect() as conn:
        get_app(conn, app_id)
        data = validate_record(get_fields(conn, app_id), body.data)
        ts = now()
        cur = conn.execute(
            "INSERT INTO records (app_id, data, created_at, updated_at) VALUES (?,?,?,?)",
            (app_id, json.dumps(data, ensure_ascii=False), ts, ts))
        return record_row(conn.execute(
            "SELECT * FROM records WHERE id = ?", (cur.lastrowid,)).fetchone())


def get_record(conn, app_id: int, record_id: int):
    row = conn.execute("SELECT * FROM records WHERE id = ? AND app_id = ?",
                       (record_id, app_id)).fetchone()
    if not row:
        raise HTTPException(404, "レコードが見つかりません")
    return row


@app.put("/api/apps/{app_id}/records/{record_id}")
def update_record(app_id: int, record_id: int, body: RecordIn):
    with connect() as conn:
        get_record(conn, app_id, record_id)
        data = validate_record(get_fields(conn, app_id), body.data)
        conn.execute("UPDATE records SET data = ?, updated_at = ? WHERE id = ?",
                     (json.dumps(data, ensure_ascii=False), now(), record_id))
        return record_row(get_record(conn, app_id, record_id))


@app.delete("/api/apps/{app_id}/records/{record_id}", status_code=204)
def delete_record(app_id: int, record_id: int):
    with connect() as conn:
        get_record(conn, app_id, record_id)
        conn.execute("DELETE FROM records WHERE id = ?", (record_id,))


@app.get("/api/apps/{app_id}/aggregate")
def aggregate(app_id: int, group: int, sum: int | None = None):
    """group 項目の値ごとに件数（と sum 項目の合計）を集計する。"""
    with connect() as conn:
        get_app(conn, app_id)
        fields = {f["id"]: f for f in get_fields(conn, app_id)}
        if group not in fields:
            raise HTTPException(400, "集計項目が正しくありません")
        if sum is not None and (sum not in fields or fields[sum]["type"] != "number"):
            raise HTTPException(400, "合計できるのは数値項目のみです")
        gf, result = fields[group], {}
        rows = conn.execute("SELECT data FROM records WHERE app_id = ?", (app_id,)).fetchall()
        for row in rows:
            data = json.loads(row["data"])
            v = data.get(str(group))
            if gf["type"] == "check":
                keys = ["オン" if v else "オフ"]
            elif gf["type"] == "multi":
                keys = v or ["（未入力）"]
            else:
                keys = ["（未入力）" if v in (None, "") else str(v)]
            amount = data.get(str(sum), 0) if sum is not None else 0
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
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ID", *[f["label"] for f in fields], "作成日時", "更新日時"])
    for row in rows:
        data = json.loads(row["data"])
        w.writerow([row["id"], *[display(f, data.get(str(f["id"]))) for f in fields],
                    row["created_at"], row["updated_at"]])
    # Excel で文字化けしないよう BOM 付き UTF-8 で出力する
    return Response(
        "﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="app-{a["id"]}.csv"'})


@app.post("/api/apps/{app_id}/import")
async def import_csv(app_id: int, file: UploadFile = File(...)):
    """CSV を取り込む。1行目を項目名として照合し、エラー行はスキップして報告する。"""
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
        cols = {f["label"]: f for f in fields}
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
                data[str(f["id"])] = cell
            try:
                cleaned = validate_record(fields, data)
            except HTTPException as e:
                labels = {str(f["id"]): f["label"] for f in fields}
                msg = "、".join(f"{labels[k]}: {m}" for k, m in e.detail["errors"].items())
                errors.append({"line": n, "message": msg})
                continue
            ts = now()
            conn.execute(
                "INSERT INTO records (app_id, data, created_at, updated_at) VALUES (?,?,?,?)",
                (app_id, json.dumps(cleaned, ensure_ascii=False), ts, ts))
            imported += 1
        return {"imported": imported, "errors": errors}


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
