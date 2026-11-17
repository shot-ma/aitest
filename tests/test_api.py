import pytest
from fastapi.testclient import TestClient


ADMIN = {"username": "admin", "display_name": "管理者", "password": "password1"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    """初期設定を済ませ、システム管理者としてログインしたクライアント。"""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/test.db")
    import app.main as m
    m._login_failures.clear()
    c = TestClient(m.app)
    assert c.post("/api/auth/setup", json=ADMIN).status_code == 200
    return c


def new_user(admin, username, display_name, role="user"):
    """管理者にユーザーを作らせ、そのユーザーでログインしたクライアントを返す。"""
    from app.main import app
    r = admin.post("/api/users", json={"username": username, "display_name": display_name,
                                       "password": "password1", "role": role})
    assert r.status_code == 201, r.text
    c = TestClient(app)
    assert c.post("/api/auth/login", json={"username": username, "password": "password1"}).status_code == 200
    c.user_id = r.json()["id"]
    return c


@pytest.fixture
def inquiry(client):
    app = client.post("/api/apps", json={"name": "問い合わせ", "template": "inquiries"}).json()
    ids = {f["label"]: str(f["id"]) for f in app["fields"]}
    return app["id"], ids


def add(client, app_id, ids, **values):
    return client.post(f"/api/apps/{app_id}/records",
                       json={"data": {ids[k]: v for k, v in values.items()}})


def test_app_crud(client):
    a = client.post("/api/apps", json={"name": "テスト"}).json()
    assert a["fields"] == []
    assert client.put(f"/api/apps/{a['id']}", json={"name": "改名"}).json()["name"] == "改名"
    assert client.get("/api/apps").json()[0]["record_count"] == 0
    assert client.delete(f"/api/apps/{a['id']}").status_code == 204
    assert client.get(f"/api/apps/{a['id']}").status_code == 404


def test_fields_replace_and_delete_cleans_data(client):
    a = client.post("/api/apps", json={"name": "テスト"}).json()
    url = f"/api/apps/{a['id']}/fields"
    fields = client.put(url, json={"fields": [
        {"label": "名前", "type": "text", "required": True},
        {"label": "色", "type": "select", "options": ["赤", "青", "赤", " "]},
    ]}).json()
    assert fields[1]["options"] == ["赤", "青"]
    name_id, color_id = fields[0]["id"], fields[1]["id"]
    client.post(f"/api/apps/{a['id']}/records",
                json={"data": {str(name_id): "A", str(color_id): "赤"}})

    # 重複ラベル・選択肢なし・タイプ変更は拒否
    assert client.put(url, json={"fields": [
        {"label": "x", "type": "text"}, {"label": "x", "type": "text"}]}).status_code == 400
    assert client.put(url, json={"fields": [{"label": "y", "type": "radio"}]}).status_code == 400
    assert client.put(url, json={"fields": [
        {"id": name_id, "label": "名前", "type": "number"}]}).status_code == 400

    # 色を削除し並び替え → レコードから値も消える
    fields = client.put(url, json={"fields": [
        {"label": "新規", "type": "check"}, {"id": name_id, "label": "氏名", "type": "text"},
    ]}).json()
    assert [f["label"] for f in fields] == ["新規", "氏名"]
    rec = client.get(f"/api/apps/{a['id']}/records").json()["records"][0]
    assert rec["data"] == {str(name_id): "A"}


def test_record_validation(client, inquiry):
    app_id, ids = inquiry
    r = add(client, app_id, ids, 件名="", 受付日="2026-13-01", 状況="謎", **{"対応工数（時間）": "abc"})
    assert r.status_code == 422
    assert set(r.json()["detail"]["errors"]) == {
        ids["件名"], ids["受付日"], ids["状況"], ids["対応工数（時間）"]}

    r = add(client, app_id, ids, 件名=" 質問 ", 受付日="2026/11/16", 状況="未対応",
            種別=["要望", "質問"], **{"対応工数（時間）": "1,500"})
    assert r.status_code == 201
    data = r.json()["data"]
    assert data[ids["件名"]] == "質問"
    assert data[ids["受付日"]] == "2026-11-16"
    assert data[ids["種別"]] == ["質問", "要望"]  # 定義順に正規化
    assert data[ids["対応工数（時間）"]] == 1500

    rid = r.json()["id"]
    r = client.put(f"/api/apps/{app_id}/records/{rid}", json={"data": {
        ids["件名"]: "更新", ids["受付日"]: "2026-11-17", ids["状況"]: "完了"}})
    assert r.json()["data"][ids["状況"]] == "完了"
    assert client.delete(f"/api/apps/{app_id}/records/{rid}").status_code == 204
    assert client.delete(f"/api/apps/{app_id}/records/{rid}").status_code == 404


def test_list_search_filter_sort_paging(client, inquiry):
    app_id, ids = inquiry
    hours = ids["対応工数（時間）"]
    for title, status, kinds, h in [("ログイン不可", "未対応", ["不具合"], 10),
                                    ("料金の質問", "完了", ["質問"], 2),
                                    ("100%表示の要望", "完了", ["要望", "質問"], 9)]:
        assert add(client, app_id, ids, 件名=title, 受付日="2026-11-01", 状況=status,
                   種別=kinds, **{"対応工数（時間）": h}).status_code == 201
    url = f"/api/apps/{app_id}/records"
    titles = lambda **p: [r["data"][ids["件名"]] for r in client.get(url, params=p).json()["records"]]

    assert titles(q="ログイン") == ["ログイン不可"]
    assert titles(q="100%") == ["100%表示の要望"]  # LIKE のワイルドカードはエスケープされる
    assert titles(q="存在しない語") == []
    assert len(titles(**{f"f_{ids['状況']}": "完了"})) == 2
    assert len(titles(**{f"f_{ids['種別']}": "質問"})) == 2
    assert titles(sort=hours, order="asc") == ["料金の質問", "100%表示の要望", "ログイン不可"]  # 数値順
    res = client.get(url, params={"per_page": 2, "page": 2, "sort": "id", "order": "asc"}).json()
    assert res["total"] == 3 and len(res["records"]) == 1


def test_aggregate(client, inquiry):
    app_id, ids = inquiry
    for status, h in [("完了", 3), ("完了", 4.5), ("未対応", None)]:
        add(client, app_id, ids, 件名="x", 受付日="2026-11-01", 状況=status,
            **{"対応工数（時間）": h})
    res = client.get(f"/api/apps/{app_id}/aggregate",
                     params={"group": ids["状況"], "sum": ids["対応工数（時間）"]}).json()
    assert res["total"] == 3
    assert res["groups"] == [{"key": "未対応", "count": 1, "sum": 0},
                             {"key": "完了", "count": 2, "sum": 7.5}]
    assert client.get(f"/api/apps/{app_id}/aggregate",
                      params={"group": ids["状況"], "sum": ids["件名"]}).status_code == 400


def test_csv_roundtrip(client, inquiry):
    app_id, ids = inquiry
    add(client, app_id, ids, 件名='カンマ,と"引用"', 受付日="2026-11-01", 状況="対応中",
        種別=["質問", "要望"], 内容="1行目\n2行目")
    exported = client.get(f"/api/apps/{app_id}/export.csv")
    assert exported.content.startswith(b"\xef\xbb\xbf")

    other = client.post("/api/apps", json={"name": "コピー", "template": "inquiries"}).json()
    r = client.post(f"/api/apps/{other['id']}/import",
                    files={"file": ("a.csv", exported.content, "text/csv")}).json()
    assert r == {"imported": 1, "errors": []}
    rec = client.get(f"/api/apps/{other['id']}/records").json()["records"][0]
    by_label = {f["label"]: rec["data"].get(str(f["id"])) for f in other["fields"]}
    assert by_label["件名"] == 'カンマ,と"引用"'
    assert by_label["種別"] == ["質問", "要望"]
    assert by_label["内容"] == "1行目\n2行目"


def test_csv_import_sjis_and_errors(client, inquiry):
    app_id, _ = inquiry
    csv_text = "件名,受付日,状況\r\nOK,2026-11-01,完了\r\n,2026-11-01,完了\r\nNG,きのう,完了\r\n"
    r = client.post(f"/api/apps/{app_id}/import",
                    files={"file": ("a.csv", csv_text.encode("cp932"), "text/csv")}).json()
    assert r["imported"] == 1
    assert [e["line"] for e in r["errors"]] == [3, 4]
    assert client.post(f"/api/apps/{app_id}/import",
                       files={"file": ("a.csv", b"a,b\n1,2\n", "text/csv")}).status_code == 400


# ---------- コメント・通知・添付ファイル・参照 ----------

def test_comments_and_notifications(client, inquiry):
    app_id, ids = inquiry  # 作成者（管理者）は自動でフォロー中
    base = f"/api/apps/{app_id}"
    sato, tanaka = new_user(client, "sato", "佐藤"), new_user(client, "tanaka", "田中")
    values = {ids["件名"]: "x", ids["受付日"]: "2026-11-01", ids["状況"]: "未対応"}

    rid = sato.post(f"{base}/records", json={"data": values}).json()["id"]
    n = client.get("/api/notifications").json()
    assert n["unread"] == 1 and "佐藤 さんがレコード No.1 を登録" in n["items"][0]["message"]
    assert sato.get("/api/notifications").json()["unread"] == 0  # 本人には届かない

    # 田中がコメント → フォロー中の管理者と、レコード作成者の佐藤に届く
    c = tanaka.post(f"{base}/records/{rid}/comments", json={"body": " 確認します "})
    assert c.status_code == 201 and c.json()["body"] == "確認します" and c.json()["author"] == "田中"
    assert sato.get("/api/notifications").json()["items"][0]["record_id"] == rid
    assert client.get("/api/notifications").json()["unread"] == 2
    # 佐藤が返信 → 以前コメントした田中にも届く
    c2 = sato.post(f"{base}/records/{rid}/comments", json={"body": "お願いします"}).json()
    assert tanaka.get("/api/notifications").json()["unread"] == 1

    assert client.get(f"{base}/records").json()["records"][0]["comment_count"] == 2
    cid = c.json()["id"]
    assert sato.delete(f"{base}/records/{rid}/comments/{cid}").status_code == 403  # 他人のコメント
    assert tanaka.delete(f"{base}/records/{rid}/comments/{cid}").status_code == 204
    assert client.delete(f"{base}/records/{rid}/comments/{c2['id']}").status_code == 204  # アプリ管理者は可
    assert client.post(f"{base}/records/{rid}/comments", json={"body": "  "}).status_code == 400

    # 既読化とフォロー解除
    assert client.post("/api/notifications/read").status_code == 204
    assert client.get("/api/notifications").json()["unread"] == 0
    assert client.get(base).json()["following"] is True
    client.put(f"{base}/follow", json={"follow": False})
    tanaka.put(f"{base}/records/{rid}", json={"data": values})
    assert client.get("/api/notifications").json()["unread"] == 0
    assert sato.get("/api/notifications").json()["unread"] == 2  # 作成者には届く

    # アプリを見られなくなった人には通知しない
    client.put(f"{base}/access", json={"default_role": "none", "members": [
        {"user_id": tanaka.user_id, "role": "editor"}]})
    tanaka.put(f"{base}/records/{rid}", json={"data": values})
    assert sato.get("/api/notifications").json()["unread"] == 2


def test_attachments(client, tmp_path):
    a = client.post("/api/apps", json={"name": "書類"}).json()
    base = f"/api/apps/{a['id']}"
    f1, f2 = client.put(f"{base}/fields", json={"fields": [
        {"label": "資料", "type": "file"}, {"label": "別紙", "type": "file"}]}).json()
    up = lambda field, name, body: client.post(
        f"{base}/fields/{field['id']}/files", files={"file": (name, body, "text/html")}).json()
    stored = lambda: {p.name for p in (tmp_path / "uploads").iterdir()}

    a1, a2 = up(f1, "見積.html", b"<script>1</script>"), up(f1, "b.txt", b"bb")
    assert a1 == {"id": a1["id"], "name": "見積.html", "size": 18}
    rec = client.post(f"{base}/records", json={"data": {str(f1["id"]): [a1["id"], a2["id"]]}}).json()
    assert rec["data"][str(f1["id"])] == [a1, {"id": a2["id"], "name": "b.txt", "size": 2}]

    dl = client.get(f"/api/files/{a1['id']}")
    assert dl.content == b"<script>1</script>"
    assert dl.headers["content-type"] == "application/octet-stream"
    assert "attachment" in dl.headers["content-disposition"]

    # 別項目のファイル・他レコードに紐付いたファイル・存在しない ID は拒否
    a3 = up(f2, "c.txt", b"c")
    bad = lambda data: client.post(f"{base}/records", json={"data": data}).status_code
    assert bad({str(f1["id"]): [a3["id"]]}) == 422
    assert bad({str(f1["id"]): [a1["id"]]}) == 422
    assert bad({str(f1["id"]): [9999]}) == 422

    # 添付を1つ外して更新 → 実ファイルも消える。検索はファイル名にもヒットする
    assert len(stored()) == 3
    client.put(f"{base}/records/{rec['id']}", json={"data": {str(f1["id"]): [{"id": a2["id"]}]}})
    assert client.get(f"/api/files/{a1['id']}").status_code == 404
    assert len(stored()) == 2
    assert client.get(f"{base}/records", params={"q": "b.txt"}).json()["total"] == 1
    assert "b.txt" in client.get(f"{base}/export.csv").text

    # レコード削除・アプリ削除でファイルも消える
    client.delete(f"{base}/records/{rec['id']}")
    assert len(stored()) == 1
    client.delete(base)
    assert stored() == set()


def test_upload_size_limit(client, monkeypatch):
    import app.main as m
    monkeypatch.setattr(m, "MAX_FILE_SIZE", 10)
    a = client.post("/api/apps", json={"name": "書類"}).json()
    f = client.put(f"/api/apps/{a['id']}/fields", json={"fields": [
        {"label": "資料", "type": "file"}]}).json()[0]
    url = f"/api/apps/{a['id']}/fields/{f['id']}/files"
    assert client.post(url, files={"file": ("a", b"x" * 11)}).status_code == 413
    assert client.post(url, files={"file": ("a", b"x" * 10)}).status_code == 201
    assert len(list(m.upload_dir().iterdir())) == 1


def test_lookup(client):
    cust = client.post("/api/apps", json={"name": "顧客", "template": "customers"}).json()
    company = next(f for f in cust["fields"] if f["label"] == "会社名")
    memo = next(f for f in cust["fields"] if f["label"] == "メモ")
    c1 = client.post(f"/api/apps/{cust['id']}/records", json={"data": {str(company["id"]): "青空商事"}}).json()
    c2 = client.post(f"/api/apps/{cust['id']}/records", json={"data": {str(company["id"]): "山田工業"}}).json()

    deals = client.post("/api/apps", json={"name": "案件"}).json()
    base = f"/api/apps/{deals['id']}"
    put = lambda fields: client.put(f"{base}/fields", json={"fields": fields})
    title = {"label": "案件名", "type": "text"}
    assert put([title, {"label": "顧客", "type": "lookup"}]).status_code == 400
    assert put([title, {"label": "顧客", "type": "lookup", "config": {
        "app_id": cust["id"], "field_id": memo["id"]}}]).status_code == 400  # 複数行は表示項目に不可
    fields = put([title, {"label": "顧客", "type": "lookup", "config": {
        "app_id": cust["id"], "field_id": company["id"]}}]).json()
    t, lk = str(fields[0]["id"]), str(fields[1]["id"])

    cands = client.get(f"{base}/fields/{lk}/candidates", params={"q": "山田"}).json()
    assert cands == [{"id": c2["id"], "label": "山田工業"}]
    assert len(client.get(f"{base}/fields/{lk}/candidates").json()) == 2

    assert client.post(f"{base}/records", json={"data": {lk: 9999}}).status_code == 422
    rec = client.post(f"{base}/records", json={"data": {t: "導入", lk: c1["id"]}}).json()
    assert rec["refs"][lk] == {"id": c1["id"], "app_id": cust["id"], "label": "青空商事",
                               "restricted": False}
    client.post(f"{base}/records", json={"data": {t: "更新", lk: {"id": c2["id"]}}})

    # 参照先を書き換えると表示も追従する
    client.put(f"/api/apps/{cust['id']}/records/{c1['id']}", json={"data": {str(company["id"]): "青空HD"}})
    assert client.get(f"{base}/records/{rec['id']}").json()["refs"][lk]["label"] == "青空HD"
    agg = client.get(f"{base}/aggregate", params={"group": lk}).json()["groups"]
    assert [(g["key"], g["count"]) for g in agg] == [("山田工業", 1), ("青空HD", 1)]

    # CSV は表示名で書き出し、表示名で照合して取り込む
    exported = client.get(f"{base}/export.csv").text
    assert "青空HD" in exported
    r = client.post(f"{base}/import", files={"file": (
        "a.csv", "案件名,顧客\n新規,山田工業\n不明,存在しない会社\n".encode(), "text/csv")}).json()
    assert r["imported"] == 1 and r["errors"][0]["line"] == 3

    # 参照先レコードが消えても壊れない
    client.delete(f"/api/apps/{cust['id']}/records/{c1['id']}")
    assert client.get(f"{base}/records/{rec['id']}").json()["refs"][lk]["label"] is None
    client.delete(f"/api/apps/{cust['id']}")
    assert client.get(f"{base}/records").json()["total"] == 3


def test_migrates_old_database(tmp_path, monkeypatch):
    import sqlite3
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE apps (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '', icon TEXT NOT NULL DEFAULT '📋', created_at TEXT NOT NULL);
        CREATE TABLE fields (id INTEGER PRIMARY KEY AUTOINCREMENT, app_id INTEGER NOT NULL, label TEXT NOT NULL,
            type TEXT NOT NULL, required INTEGER NOT NULL DEFAULT 0, options TEXT NOT NULL DEFAULT '[]',
            position INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE records (id INTEGER PRIMARY KEY AUTOINCREMENT, app_id INTEGER NOT NULL,
            data TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        INSERT INTO apps (name, created_at) VALUES ('旧', '2026-01-01 00:00:00');
        INSERT INTO fields (app_id, label, type) VALUES (1, '名前', 'text');
        INSERT INTO records (app_id, data, created_at, updated_at) VALUES (1, '{"1":"A"}', 'x', 'x');
    """)
    conn.commit()
    conn.close()
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    from app.main import app
    c = TestClient(app)
    assert c.get("/api/auth/state").json() == {"setup_required": True, "user": None}
    c.post("/api/auth/setup", json=ADMIN)
    rec = c.get("/api/apps/1/records").json()["records"][0]
    assert rec["data"] == {"1": "A"} and rec["comment_count"] == 0
    assert c.put("/api/apps/1/records/1", json={"data": {"1": "B"}}).json()["updated_by"] == "管理者"
    # 既存アプリは全員が編集でき、管理はシステム管理者が行う
    u = new_user(c, "ippan", "一般")
    assert u.get("/api/apps/1").json()["role"] == "editor"


# ---------- ログインと権限 ----------

def test_auth_flow(client):
    from app.main import app
    anon = TestClient(app)
    for path in ("/api/apps", "/api/meta", "/api/notifications", "/api/files/1", "/api/apps/1/export.csv"):
        assert anon.get(path).status_code == 401
    assert anon.post("/api/apps", json={"name": "x"}).status_code == 401
    assert anon.get("/api/auth/state").json() == {"setup_required": False, "user": None}
    assert anon.post("/api/auth/setup", json={**ADMIN, "username": "evil"}).status_code == 403  # 初期設定は一度だけ

    # ユーザー作成の入力チェック
    bad = lambda **kw: client.post("/api/users", json={"username": "taro", "display_name": "太郎",
                                                       "password": "password1", **kw}).status_code
    assert bad(password="short") == 400
    assert bad(username="a b") == 400
    assert bad(username="ADMIN") == 400  # 大文字小文字違いの重複
    assert bad(role="root") == 400

    taro = new_user(client, "taro", "太郎")
    assert taro.get("/api/auth/state").json()["user"] == {
        "id": taro.user_id, "username": "taro", "display_name": "太郎", "role": "user"}
    assert "password" not in str(client.get("/api/users").json())
    assert taro.get("/api/users").status_code == 403
    assert taro.post("/api/users", json={"username": "x12", "display_name": "x", "password": "password1"}).status_code == 403
    assert taro.put(f"/api/users/{taro.user_id}", json={"display_name": "太郎", "role": "admin", "active": True}).status_code == 403

    # パスワード変更: 現在のパスワードが必要。他の端末のログインは切れる
    other = TestClient(app)
    other.post("/api/auth/login", json={"username": "taro", "password": "password1"})
    assert taro.put("/api/auth/password", json={"current": "wrong", "new": "newpassword"}).status_code == 400
    assert taro.put("/api/auth/password", json={"current": "password1", "new": "short"}).status_code == 400
    assert taro.put("/api/auth/password", json={"current": "password1", "new": "newpassword"}).status_code == 204
    assert taro.get("/api/apps").status_code == 200
    assert other.get("/api/apps").status_code == 401
    assert other.post("/api/auth/login", json={"username": "taro", "password": "password1"}).status_code == 401
    assert other.post("/api/auth/login", json={"username": "Taro ", "password": "newpassword"}).status_code == 200

    assert taro.put("/api/auth/profile", json={"display_name": "山田太郎"}).json()["display_name"] == "山田太郎"

    # ログアウト、無効化
    assert other.post("/api/auth/logout").status_code == 204
    assert other.get("/api/apps").status_code == 401
    upd = lambda uid, **kw: client.put(f"/api/users/{uid}", json={"display_name": "太郎", "role": "user", "active": True, **kw})
    assert upd(taro.user_id, active=False).status_code == 200
    assert taro.get("/api/apps").status_code == 401
    assert other.post("/api/auth/login", json={"username": "taro", "password": "newpassword"}).status_code == 401
    # 管理者によるパスワード再設定
    assert upd(taro.user_id, password="resetpass1").status_code == 200
    assert other.post("/api/auth/login", json={"username": "taro", "password": "resetpass1"}).status_code == 200
    # 最後の管理者は降格も無効化もできない
    admin_id = client.get("/api/auth/state").json()["user"]["id"]
    assert upd(admin_id).status_code == 400
    assert upd(admin_id, role="admin", active=False).status_code == 400
    assert upd(taro.user_id, role="admin").status_code == 200
    assert upd(admin_id).status_code == 200


def test_login_throttle_and_origin(client):
    from app.main import app
    c = TestClient(app)
    wrong = lambda: c.post("/api/auth/login", json={"username": "admin", "password": "x"}).status_code
    assert [wrong() for _ in range(5)] == [401] * 5
    assert wrong() == 429
    assert c.post("/api/auth/login", json={"username": "admin", "password": "password1"}).status_code == 429
    # 他サイトからの書き込みは拒否、同一サイトは許可
    assert client.post("/api/apps", json={"name": "x"}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/apps", json={"name": "x"}, headers={"Origin": "http://testserver"}).status_code == 201


def test_app_permissions(client):
    owner = new_user(client, "owner", "持ち主")
    viewer, editor, outsider = (new_user(client, n, n) for n in ("viewer", "editor", "outsider"))
    a = owner.post("/api/apps", json={"name": "案件", "template": "inquiries"}).json()
    assert a["role"] == "manager"
    base = f"/api/apps/{a['id']}"
    ids = {f["label"]: str(f["id"]) for f in a["fields"]}
    values = {ids["件名"]: "x", ids["受付日"]: "2026-11-01", ids["状況"]: "未対応"}
    rid = owner.post(f"{base}/records", json={"data": values}).json()["id"]

    # 既定は全員が編集可。設定は管理者だけ
    assert outsider.get(base).json()["role"] == "editor"
    assert outsider.put(base, json={"name": "乗っ取り"}).status_code == 403
    assert outsider.get(f"{base}/access").status_code == 403

    # 自分の管理権限は外せない / 不正な値は拒否
    put = lambda who, default, members: who.put(f"{base}/access", json={"default_role": default, "members": members})
    me = {"user_id": owner.user_id, "role": "manager"}
    assert put(owner, "none", []).status_code == 400
    assert put(owner, "manager", [me]).status_code == 400
    assert put(owner, "none", [me, {"user_id": 9999, "role": "viewer"}]).status_code == 400
    r = put(owner, "none", [me, {"user_id": viewer.user_id, "role": "viewer"},
                            {"user_id": editor.user_id, "role": "editor"}])
    assert r.status_code == 200 and len(r.json()["members"]) == 3

    # 権限のない人: 一覧に出ず、存在も分からない
    assert [x["id"] for x in outsider.get("/api/apps").json()] == []
    for call in (outsider.get(base), outsider.get(f"{base}/records"), outsider.get(f"{base}/export.csv"),
                 outsider.post(f"{base}/records", json={"data": values}),
                 outsider.get(f"{base}/records/{rid}/comments"), outsider.put(f"{base}/follow", json={"follow": True})):
        assert call.status_code == 404

    # 閲覧のみ: 読めるが書けない
    assert viewer.get(base).json()["role"] == "viewer"
    assert viewer.get(f"{base}/records").json()["total"] == 1
    assert viewer.get(f"{base}/aggregate", params={"group": ids["状況"]}).status_code == 200
    assert viewer.get(f"{base}/export.csv").status_code == 200
    for call in (viewer.post(f"{base}/records", json={"data": values}),
                 viewer.put(f"{base}/records/{rid}", json={"data": values}),
                 viewer.delete(f"{base}/records/{rid}"),
                 viewer.post(f"{base}/records/{rid}/comments", json={"body": "x"}),
                 viewer.post(f"{base}/import", files={"file": ("a.csv", "件名\nx\n".encode(), "text/csv")}),
                 viewer.put(f"{base}/fields", json={"fields": []}),
                 viewer.delete(base)):
        assert call.status_code == 403

    # 編集可: レコードとコメントは扱えるが、項目・権限・アプリ削除は不可
    assert editor.put(f"{base}/records/{rid}", json={"data": values}).status_code == 200
    assert editor.post(f"{base}/records/{rid}/comments", json={"body": "x"}).status_code == 201
    for call in (editor.put(f"{base}/fields", json={"fields": []}), editor.get(f"{base}/access"),
                 editor.put(f"{base}/access", json={"default_role": "editor", "members": []}),
                 editor.delete(base)):
        assert call.status_code == 403

    # システム管理者は全アプリを管理できる。アプリ管理者は削除できる
    assert client.get(base).json()["role"] == "manager"
    assert put(client, "viewer", []).status_code == 200
    assert outsider.get(base).json()["role"] == "viewer"
    assert owner.get(base).json()["role"] == "viewer"
    put(client, "none", [me])
    assert owner.delete(base).status_code == 204


def test_permissions_on_files_and_lookups(client):
    member = new_user(client, "member", "メンバー")
    outsider = new_user(client, "outsider", "部外者")
    # 顧客アプリ（限定公開）と、それを参照する案件アプリ（全員編集可）
    cust = client.post("/api/apps", json={"name": "顧客", "template": "customers"}).json()
    company = next(f for f in cust["fields"] if f["label"] == "会社名")
    cbase = f"/api/apps/{cust['id']}"
    fields = client.put(f"{cbase}/fields", json={"fields": [
        {"id": company["id"], "label": "会社名", "type": "text"}, {"label": "契約書", "type": "file"}]}).json()
    up = client.post(f"{cbase}/fields/{fields[1]['id']}/files", files={"file": ("秘密.txt", b"secret")}).json()
    c1 = client.post(f"{cbase}/records", json={"data": {str(company["id"]): "青空商事", str(fields[1]["id"]): [up["id"]]}}).json()
    client.put(f"{cbase}/access", json={"default_role": "none", "members": [{"user_id": member.user_id, "role": "viewer"}]})

    deals = client.post("/api/apps", json={"name": "案件"}).json()
    dbase = f"/api/apps/{deals['id']}"
    lookup = {"label": "顧客", "type": "lookup", "config": {"app_id": cust["id"], "field_id": company["id"]}}
    lk = str(client.put(f"{dbase}/fields", json={"fields": [lookup]}).json()[0]["id"])
    rec = member.post(f"{dbase}/records", json={"data": {lk: c1["id"]}}).json()

    # 添付ファイル: 閲覧権限のある人だけがダウンロード・アップロードできる
    assert member.get(f"/api/files/{up['id']}").content == b"secret"
    assert outsider.get(f"/api/files/{up['id']}").status_code == 404
    assert member.post(f"{cbase}/fields/{fields[1]['id']}/files", files={"file": ("a", b"a")}).status_code == 403
    assert outsider.post(f"{cbase}/fields/{fields[1]['id']}/files", files={"file": ("a", b"a")}).status_code == 404

    # 参照: 参照先を見られない人には内容を出さず、候補検索も新しい値の設定もできない
    assert member.get(f"{dbase}/records/{rec['id']}").json()["refs"][lk]["label"] == "青空商事"
    hidden = outsider.get(f"{dbase}/records/{rec['id']}").json()["refs"][lk]
    assert hidden["label"] is None and hidden["restricted"] is True
    assert "青空商事" not in outsider.get(f"{dbase}/export.csv").text
    assert outsider.get(f"{dbase}/aggregate", params={"group": lk}).json()["groups"][0]["key"] == "（権限なし）"
    assert outsider.get(f"{dbase}/fields/{lk}/candidates").status_code == 403
    assert outsider.post(f"{dbase}/records", json={"data": {lk: c1["id"]}}).status_code == 422
    assert outsider.put(f"{dbase}/records/{rec['id']}", json={"data": {lk: c1["id"]}}).status_code == 200  # 既存値の維持は可

    # 参照先を見られない人は、そのアプリを参照する項目を作れない
    own = outsider.post("/api/apps", json={"name": "自分用"}).json()
    assert outsider.put(f"/api/apps/{own['id']}/fields", json={"fields": [lookup]}).status_code == 400


def test_date_range_filter(client, inquiry):
    app_id, ids = inquiry
    for d in ("2026-10-31", "2026-11-01", "2026-11-30", "2026-12-01"):
        add(client, app_id, ids, 件名=d, 受付日=d, 状況="未対応")
    add(client, app_id, ids, 件名="完了分", 受付日="2026-11-15", 状況="完了")
    key = f"f_{ids['受付日']}"
    get = lambda **p: sorted(r["data"][ids["件名"]] for r in client.get(
        f"/api/apps/{app_id}/records", params=p).json()["records"])
    assert get(**{f"{key}_from": "2026-11-01", f"{key}_to": "2026-11-30"}) == ["2026-11-01", "2026-11-30", "完了分"]
    assert get(**{f"{key}_from": "2026-12-01"}) == ["2026-12-01"]
    # ほかの絞り込みと組み合わせられる
    assert get(**{f"{key}_from": "2026-11-01", f"{key}_to": "2026-11-30", f"f_{ids['状況']}": "完了"}) == ["完了分"]


def test_views(client, inquiry):
    app_id, ids = inquiry
    base = f"/api/apps/{app_id}"
    sato, tanaka = new_user(client, "sato", "佐藤"), new_user(client, "tanaka", "田中")
    n = lambda label: int(ids[label])
    table = {"name": "未対応だけ", "type": "table", "config": {
        "columns": [n("件名"), n("状況"), 9999, n("件名")], "sort": ids["受付日"], "order": "asc",
        "filters": {ids["状況"]: "未対応", ids["件名"]: "x", "9999": "y"}}}
    assert sato.post(f"{base}/views", json=table).status_code == 403  # 管理者だけが保存できる
    v1 = client.post(f"{base}/views", json=table).json()["id"]
    views = client.get(base).json()["views"]
    assert views == [{"id": v1, "name": "未対応だけ", "type": "table", "access": "all", "user_ids": [],
                      "config": {"columns": [n("件名"), n("状況")], "sort": ids["受付日"], "order": "asc",
                                 "filters": {ids["状況"]: "未対応"}}}]

    # 入力チェック
    bad = lambda **kw: client.post(f"{base}/views", json={**table, **kw}).status_code
    assert bad(name=" ") == 400
    assert bad(type="chart") == 400
    assert bad(config={"columns": []}) == 400
    assert bad(type="calendar", config={"date_field": n("件名")}) == 400
    assert bad(access="restricted", user_ids=[9999]) == 400
    assert client.put(f"{base}/views/{v1}", json={**table, "type": "calendar",
                                                  "config": {"date_field": n("受付日")}}).status_code == 400

    # 限定公開のカレンダービューを既定（先頭）として追加
    cal = {"name": "受付カレンダー", "type": "calendar", "access": "restricted", "user_ids": [sato.user_id],
           "make_default": True, "config": {"date_field": n("受付日"), "title_field": n("状況")}}
    v2 = client.post(f"{base}/views", json=cal).json()["id"]
    assert [v["id"] for v in client.get(base).json()["views"]] == [v2, v1]
    assert [v["id"] for v in sato.get(base).json()["views"]] == [v2, v1]
    assert [v["id"] for v in tanaka.get(base).json()["views"]] == [v1]  # 指定されていない人には出ない
    assert "user_ids" not in sato.get(base).json()["views"][0]  # 公開範囲は管理者にだけ返す

    # 更新（公開範囲の変更）と、項目削除後の自動整理
    client.put(f"{base}/views/{v2}", json={**cal, "access": "all", "make_default": False})
    assert [v["id"] for v in tanaka.get(base).json()["views"]] == [v2, v1]
    fields = [f for f in client.get(base).json()["fields"] if f["label"] not in ("状況", "受付日")]
    client.put(f"{base}/fields", json={"fields": fields})
    after = {v["id"]: v["config"] for v in client.get(base).json()["views"]}
    assert after[v1] == {"columns": [n("件名")], "sort": "id", "order": "asc", "filters": {}}
    assert after[v2] == {"date_field": None, "title_field": None, "filters": {}}

    assert sato.delete(f"{base}/views/{v1}").status_code == 403
    assert client.delete(f"{base}/views/{v1}").status_code == 204
    assert client.delete(f"{base}/views/{v1}").status_code == 404
    other = client.post("/api/apps", json={"name": "別"}).json()
    assert client.delete(f"/api/apps/{other['id']}/views/{v2}").status_code == 404  # 別アプリ経由では消せない


def test_field_permissions(client, tmp_path):
    """閲覧を制限した項目は、権限のない人にはどの経路でも返さない。"""
    sato, tanaka = new_user(client, "sato", "佐藤"), new_user(client, "tanaka", "田中")
    a = client.post("/api/apps", json={"name": "社員"}).json()
    base = f"/api/apps/{a['id']}"
    only_sato = {"access": "restricted", "user_ids": [sato.user_id]}
    spec = [{"label": "氏名", "type": "text", "required": True},
            {"label": "年収", "type": "number", "required": True, **only_sato},
            {"label": "評価", "type": "select", "options": ["A", "B"], **only_sato},
            {"label": "査定メモ", "type": "text", **only_sato},
            {"label": "契約書", "type": "file", **only_sato},
            {"label": "入社日", "type": "date"}]
    assert client.put(f"{base}/fields", json={"fields": [{**spec[0], "access": "x"}]}).status_code == 400
    assert client.put(f"{base}/fields", json={"fields": [{**spec[1], "user_ids": [9999]}]}).status_code == 400
    fields = client.put(f"{base}/fields", json={"fields": spec}).json()
    assert fields[1]["access"] == "restricted" and fields[1]["user_ids"] == [sato.user_id]
    name, salary, grade, memo, doc, joined = (str(f["id"]) for f in fields)
    up = client.post(f"{base}/fields/{doc}/files", files={"file": ("契約.txt", b"secret")}).json()
    rec = client.post(f"{base}/records", json={"data": {
        name: "山田", salary: 5000000, grade: "A", memo: "昇格候補", doc: [up["id"]], joined: "2020-04-01"}}).json()
    url = f"{base}/records/{rec['id']}"

    # アプリ情報: 権限のない人には項目自体を返さない。公開範囲の設定は管理者にだけ返す
    assert [f["label"] for f in tanaka.get(base).json()["fields"]] == ["氏名", "入社日"]
    sato_fields = sato.get(base).json()["fields"]
    assert len(sato_fields) == 6 and "user_ids" not in sato_fields[1] and "access" not in sato_fields[1]
    assert "user_ids" in client.get(base).json()["fields"][1]

    # レコード: 値を返さない
    assert tanaka.get(url).json()["data"] == {name: "山田", joined: "2020-04-01"}
    assert tanaka.get(f"{base}/records").json()["records"][0]["data"] == {name: "山田", joined: "2020-04-01"}
    assert sato.get(url).json()["data"][salary] == 5000000

    # 検索・絞り込み・並び替え・集計・CSV・添付からも漏れない
    total = lambda who, **p: who.get(f"{base}/records", params=p).json()["total"]
    assert total(sato, q="昇格") == 1 and total(tanaka, q="昇格") == 0
    assert total(sato, q="契約.txt") == 1 and total(tanaka, q="契約.txt") == 0
    assert total(tanaka, q="5000000") == 0 and total(tanaka, q="山田") == 1
    assert total(sato, **{f"f_{grade}": "B"}) == 0 and total(tanaka, **{f"f_{grade}": "B"}) == 1  # 絞り込みは無視される
    assert tanaka.get(f"{base}/aggregate", params={"group": grade}).status_code == 400
    assert tanaka.get(f"{base}/aggregate", params={"group": joined, "sum": salary}).status_code == 400
    assert sato.get(f"{base}/aggregate", params={"group": grade, "sum": salary}).json()["groups"][0]["sum"] == 5000000
    csv_text = tanaka.get(f"{base}/export.csv").text
    assert "年収" not in csv_text and "5000000" not in csv_text and "昇格候補" not in csv_text and "山田" in csv_text
    assert "5000000" in sato.get(f"{base}/export.csv").text
    assert sato.get(f"/api/files/{up['id']}").content == b"secret"
    assert tanaka.get(f"/api/files/{up['id']}").status_code == 404
    assert tanaka.post(f"{base}/fields/{doc}/files", files={"file": ("a", b"a")}).status_code == 404

    # 編集: 見えない項目は書き換えられず、今の値が保たれる（添付ファイルも消えない）
    r = tanaka.put(url, json={"data": {name: "山田太郎", joined: "2020-04-01", salary: 1, grade: "B", doc: []}})
    assert r.status_code == 200 and r.json()["data"] == {name: "山田太郎", joined: "2020-04-01"}
    kept = client.get(url).json()["data"]
    assert kept[name] == "山田太郎" and kept[salary] == 5000000 and kept[grade] == "A" and kept[doc][0]["id"] == up["id"]
    assert len(list((tmp_path / "uploads").iterdir())) == 1
    # 新規登録: 見えない必須項目は空のまま登録できる。CSV 取り込みでも見えない列は無視される
    new = tanaka.post(f"{base}/records", json={"data": {name: "新人", salary: 999}})
    assert new.status_code == 201 and salary not in client.get(f"{base}/records/{new.json()['id']}").json()["data"]
    imp = tanaka.post(f"{base}/import", files={"file": ("a.csv", "氏名,年収\n鈴木,123\n".encode(), "text/csv")}).json()
    assert imp == {"imported": 1, "errors": []}
    assert total(client, q="123") == 0
    assert sato.post(f"{base}/records", json={"data": {name: "x"}}).status_code == 422  # 見える人には必須

    # ビュー: 見えない項目は列や絞り込みから外れる
    client.post(f"{base}/views", json={"name": "v", "type": "table", "config": {
        "columns": [int(name), int(salary)], "sort": salary, "filters": {grade: "A"}}})
    assert tanaka.get(base).json()["views"][0]["config"] == {
        "columns": [int(name)], "sort": "id", "order": "desc", "filters": {}}
    assert sato.get(base).json()["views"][0]["config"]["columns"] == [int(name), int(salary)]

    # 他アプリからの参照: 表示項目が見えない人には内容を出さない
    other = client.post("/api/apps", json={"name": "面談"}).json()
    obase = f"/api/apps/{other['id']}"
    lk = str(client.put(f"{obase}/fields", json={"fields": [{"label": "査定", "type": "lookup", "config": {
        "app_id": a["id"], "field_id": int(memo)}}]}).json()[0]["id"])
    orec = client.post(f"{obase}/records", json={"data": {lk: rec["id"]}}).json()
    assert sato.get(f"{obase}/records/{orec['id']}").json()["refs"][lk]["label"] == "昇格候補"
    hidden = tanaka.get(f"{obase}/records/{orec['id']}").json()["refs"][lk]
    assert hidden["label"] is None and hidden["restricted"] is True
    assert tanaka.get(f"{obase}/fields/{lk}/candidates").status_code == 403
    assert "昇格候補" not in tanaka.get(f"{obase}/export.csv").text

    # 制限を外すと見えるようになる
    client.put(f"{base}/fields", json={"fields": [{**f, "access": "all", "user_ids": []} for f in fields]})
    assert tanaka.get(url).json()["data"][salary] == 5000000


def test_field_edit_permissions(client, tmp_path):
    """編集を制限した項目は、見えるが値を変えられない。"""
    sato, tanaka = new_user(client, "sato", "佐藤"), new_user(client, "tanaka", "田中")
    a = client.post("/api/apps", json={"name": "案件"}).json()
    base = f"/api/apps/{a['id']}"
    only_sato = {"edit_access": "restricted", "editor_ids": [sato.user_id]}
    spec = [{"label": "案件名", "type": "text", "required": True},
            {"label": "承認", "type": "select", "options": ["未", "済"], "required": True, **only_sato},
            {"label": "稟議書", "type": "file", **only_sato}]
    assert client.put(f"{base}/fields", json={"fields": [{**spec[0], "edit_access": "x"}]}).status_code == 400
    assert client.put(f"{base}/fields", json={"fields": [{**spec[1], "editor_ids": [9999]}]}).status_code == 400
    fields = client.put(f"{base}/fields", json={"fields": spec}).json()
    assert fields[1]["edit_access"] == "restricted" and fields[1]["editor_ids"] == [sato.user_id]
    title, approval, doc = (str(f["id"]) for f in fields)

    # アプリ情報: 編集できるかどうかだけを返し、誰が編集できるかの設定は管理者にだけ返す
    editable = lambda who: [f["editable"] for f in who.get(base).json()["fields"]]
    assert editable(client) == [True, True, True]
    assert editable(sato) == [True, True, True]
    assert editable(tanaka) == [True, False, False]
    assert "editor_ids" not in tanaka.get(base).json()["fields"][1]
    assert "editor_ids" in client.get(base).json()["fields"][1]

    up = sato.post(f"{base}/fields/{doc}/files", files={"file": ("稟議.txt", b"doc")}).json()
    rec = sato.post(f"{base}/records", json={"data": {title: "導入", approval: "未", doc: [up["id"]]}}).json()
    url = f"{base}/records/{rec['id']}"

    # 閲覧のみの人: 値は見えるが、変更は無視されて今の値が保たれる
    assert tanaka.get(url).json()["data"][approval] == "未"
    assert tanaka.get(f"/api/files/{up['id']}").content == b"doc"
    r = tanaka.put(url, json={"data": {title: "導入（改）", approval: "済", doc: []}})
    assert r.status_code == 200
    assert r.json()["data"] == {title: "導入（改）", approval: "未", doc: [{"id": up["id"], "name": "稟議.txt", "size": 3}]}
    assert tanaka.put(url, json={"data": {title: "導入（改）"}}).json()["data"][approval] == "未"  # 送らなくても消えない
    assert len(list((tmp_path / "uploads").iterdir())) == 1
    assert tanaka.post(f"{base}/fields/{doc}/files", files={"file": ("a", b"a")}).status_code == 403
    # 新規登録と CSV 取り込みでは空のままになる（必須でもエラーにしない）
    new = tanaka.post(f"{base}/records", json={"data": {title: "新規", approval: "済"}})
    assert new.status_code == 201 and approval not in new.json()["data"]
    imp = tanaka.post(f"{base}/import", files={"file": ("a.csv", "案件名,承認\n取込,済\n".encode(), "text/csv")}).json()
    assert imp == {"imported": 1, "errors": []}
    assert client.get(f"{base}/records", params={f"f_{approval}": "済"}).json()["total"] == 0

    # 編集を許可された人と管理者は変更できる。見える人には必須も効く
    assert sato.put(url, json={"data": {title: "導入", approval: "済"}}).json()["data"][approval] == "済"
    assert sato.post(f"{base}/records", json={"data": {title: "x"}}).status_code == 422
    assert client.put(url, json={"data": {title: "導入", approval: "未"}}).json()["data"][approval] == "未"


def test_comment_attachments(client, inquiry, tmp_path):
    app_id, ids = inquiry
    base = f"/api/apps/{app_id}"
    sato = new_user(client, "sato", "佐藤")
    rid = client.post(f"{base}/records", json={"data": {ids["件名"]: "x", ids["受付日"]: "2026-11-01", ids["状況"]: "未対応"}}).json()["id"]
    url = f"{base}/records/{rid}"
    up = lambda who, name, body=b"data": who.post(f"{url}/comment-files", files={"file": (name, body)})
    stored = lambda: len(list((tmp_path / "uploads").iterdir()))

    f1, f2 = up(client, "議事録.pdf").json(), up(client, "写真.png", b"png").json()
    # 投稿前の仮ファイルは本人だけがダウンロードできる
    assert client.get(f"/api/files/{f1['id']}").content == b"data"
    assert sato.get(f"/api/files/{f1['id']}").status_code == 404
    # 他人の仮ファイルや存在しないファイルは添付できない。本文も添付もないコメントは不可
    assert sato.post(f"{url}/comments", json={"body": "x", "file_ids": [f1["id"]]}).status_code == 400
    assert client.post(f"{url}/comments", json={"body": "x", "file_ids": [9999]}).status_code == 400
    assert client.post(f"{url}/comments", json={"body": " ", "file_ids": []}).status_code == 400

    c = client.post(f"{url}/comments", json={"body": "資料です", "file_ids": [f1["id"], f2["id"]]})
    assert c.status_code == 201
    assert [f["name"] for f in c.json()["files"]] == ["議事録.pdf", "写真.png"]
    assert client.post(f"{url}/comments", json={"body": "x", "file_ids": [f1["id"]]}).status_code == 400  # 使い回し不可
    # 本文なしでファイルだけのコメントも可
    f3 = up(sato, "メモ.txt").json()
    c2 = sato.post(f"{url}/comments", json={"file_ids": [f3["id"]]}).json()
    assert c2["body"] == "" and c2["files"][0]["name"] == "メモ.txt"
    notes = client.get("/api/notifications").json()["items"]
    assert "（ファイルを添付）" in notes[0]["message"]

    listed = client.get(f"{url}/comments").json()
    assert [len(x["files"]) for x in listed] == [2, 1]
    assert sato.get(f"/api/files/{f1['id']}").content == b"data"  # 投稿後はアプリを見られる人全員
    # 閲覧のみの人は添付できない。アプリを見られない人はダウンロードできない
    client.put(f"{base}/access", json={"default_role": "viewer", "members": []})
    assert up(sato, "a.txt").status_code == 403
    client.put(f"{base}/access", json={"default_role": "none", "members": []})
    assert sato.get(f"/api/files/{f1['id']}").status_code == 404
    client.put(f"{base}/access", json={"default_role": "editor", "members": []})

    # レコード側の添付ファイル更新ではコメントの添付は消えない
    client.put(url, json={"data": {ids["件名"]: "y", ids["受付日"]: "2026-11-01", ids["状況"]: "未対応"}})
    assert stored() == 3
    # コメント削除・レコード削除でファイルも消える
    client.delete(f"{url}/comments/{c.json()['id']}")
    assert stored() == 1 and client.get(f"/api/files/{f1['id']}").status_code == 404
    up(client, "未投稿.txt")
    client.delete(url)
    assert stored() == 1  # 未投稿の仮ファイルだけが残る（1日後に自動で掃除される）
    assert client.get(f"/api/files/{f3['id']}").status_code == 404


def test_dashboard(client, inquiry):
    app_id, ids = inquiry
    base = f"/api/apps/{app_id}"
    sato = new_user(client, "sato", "佐藤")
    view = client.post(f"{base}/views", json={"name": "未対応", "type": "table", "config": {"columns": [int(ids["件名"])]}}).json()["id"]
    secret = client.post(f"{base}/views", json={"name": "管理者用", "type": "calendar", "access": "restricted",
                                                "user_ids": [], "config": {"date_field": int(ids["受付日"])}}).json()["id"]
    w = lambda **kw: {"app_id": app_id, "x": 0, "y": 0, **kw}

    # 最初は空。一般ユーザーは既定を変更できない
    assert client.get("/api/dashboard").json() == {"cols": 5, "rows": 10, "custom": False, "widgets": []}
    assert sato.put("/api/dashboard/default", json={"widgets": []}).status_code == 403
    assert sato.get("/api/dashboard/default").status_code == 403

    # 配置の検証: はみ出し・重なり・存在しないビュー・権限のないビュー
    put = lambda who, widgets, url="/api/dashboard": who.put(url, json={"widgets": widgets})
    assert put(client, [w(x=4, w=2)]).status_code == 400
    assert put(client, [w(y=9, h=2)]).status_code == 400
    assert put(client, [w(x=-1)]).status_code == 400
    assert put(client, [w(w=2, h=2), w(x=1, y=1)]).status_code == 400
    assert put(client, [w(view_id=9999)]).status_code == 400
    assert put(client, [w(mode="chart")]).status_code == 400
    assert put(sato, [w(view_id=secret)]).status_code == 400

    # 管理者が既定を設定 → 変更していない全員に表示される。権限のないビューは省かれる
    default = [w(view_id=view, w=3, h=2), w(view_id=secret, x=3, w=2, h=4), w(mode="calendar", y=2, w=3, h=3)]
    r = put(client, default, "/api/dashboard/default")
    assert r.status_code == 200 and len(r.json()["widgets"]) == 3
    mine = sato.get("/api/dashboard").json()
    assert mine["custom"] is False
    assert [(x["view_name"], x["mode"]) for x in mine["widgets"]] == [("未対応", "list"), ("カレンダー（標準）", "calendar")]
    assert mine["widgets"][0]["app_name"] == "問い合わせ"
    assert len(client.get("/api/dashboard").json()["widgets"]) == 3  # 管理者自身も既定を使う

    # 自分用に変更 → 既定を変えても影響しない。既定に戻すと既定が表示される
    assert put(sato, [w(mode="list", x=1, y=5, w=4, h=5)]).json()["custom"] is True
    put(client, [w(view_id=view)], "/api/dashboard/default")
    assert [(x["x"], x["y"], x["w"], x["h"]) for x in sato.get("/api/dashboard").json()["widgets"]] == [(1, 5, 4, 5)]
    back = sato.delete("/api/dashboard").json()
    assert back["custom"] is False and [x["view_id"] for x in back["widgets"]] == [view]

    # ビューを削除すると、配置からも自動で消える
    client.delete(f"{base}/views/{view}")
    assert sato.get("/api/dashboard").json()["widgets"] == []

    # 置けるものの一覧: 使えるビューだけ
    opts = sato.get("/api/dashboard/options").json()
    assert opts == [{"app_id": app_id, "name": "問い合わせ", "icon": "📋", "has_date": True, "views": []}]
    assert [v["name"] for v in client.get("/api/dashboard/options").json()[0]["views"]] == ["管理者用"]
