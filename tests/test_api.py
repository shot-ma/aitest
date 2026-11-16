import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/test.db")
    from app.main import app
    return TestClient(app)


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
