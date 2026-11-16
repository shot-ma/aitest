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
