"""身材参数：校验、BMI、合身结论、接口与隐私边界。

这一层最该钉死的是三件事：

1. **范围校验**——178 打成 1178、88 打成 888 必须被挡住，不能算出荒唐的推荐；
2. **体重不参与选码**——尺码表是按围度定的，用体重推码数就是编；
3. **没数据就说没数据**——没填身材、没录尺码表、缺维度，三种情况各说各的，不许猜。
"""

from __future__ import annotations

import json

import pytest

from app import auth, db
from app.config import settings
from app.domain import body as body_mod
from app.domain import size

CHART = {
    "brand": "t",
    "display_name": "T",
    "chart_version": "test",
    "source": "测试表，非官方",
    "fit": "正码",
    "categories": {
        "JSK": {
            "key": ["bust", "waist"],
            "ease": {"bust": 4, "waist": 2},
            "sizes": {
                "S": {"bust": 80, "waist": 64, "length": 88},
                "M": {"bust": 84, "waist": 68, "length": 92},
                "L": {"bust": 88, "waist": 72, "length": 96},
            },
        },
        "SK": {
            "key": ["waist"],
            "ease": {"waist": 2},
            "sizes": {"S": {"waist": 64, "length": 55}, "M": {"waist": 68, "length": 57}},
        },
    },
}


@pytest.fixture()
def charts(workdir):
    folder = workdir / "size"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "t.json").write_text(json.dumps(CHART, ensure_ascii=False), encoding="utf-8")
    return size.load_charts(folder)      # 返回 dict，不是 (dict, problems)


@pytest.fixture()
def ready_db(data_dir):
    db.init_db(settings.db_path)
    return settings.db_path


def _auth_headers(client) -> dict:
    _user, token, _claimed = auth.register("body-user", "password123")
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------- 校验


def test_sanitize_accepts_reasonable_values():
    clean, problems = body_mod.sanitize({"height": 162, "bust": 86.35, "weight": 50})
    assert problems == []
    # 86.35 的二进制表示略小于 86.35，round(…, 1) 会得到 86.3 —— 只要求"一位小数"
    assert clean == {"height": 162.0, "bust": 86.3, "weight": 50.0}


def test_sanitize_rejects_typos():
    """178 打成 1178、88 打成 888 —— 必须挡住，而不是算出一个荒唐的推荐。"""
    _clean, problems = body_mod.sanitize({"height": 1178, "bust": 888})
    assert any("身高" in item for item in problems)
    assert any("胸围" in item for item in problems)


def test_sanitize_rejects_non_numbers():
    _clean, problems = body_mod.sanitize({"waist": "很细", "hip": True})
    assert len(problems) == 2


def test_sanitize_null_means_clear():
    clean, problems = body_mod.sanitize({"waist": None, "hip": ""})
    assert problems == []
    assert clean == {"waist": None, "hip": None}


def test_sanitize_ignores_unknown_fields():
    clean, _problems = body_mod.sanitize({"cup": "C"})
    assert clean == {}


# --------------------------------------------------------------------- BMI / 衣长


@pytest.mark.parametrize("height, weight, band", [
    (160, 45, "偏瘦"),
    (160, 52, "正常"),
    (160, 65, "偏胖"),
    (160, 80, "肥胖"),
])
def test_bmi_bands(height, weight, band):
    assert body_mod.bmi(height, weight)["band"] == band


def test_bmi_needs_both_numbers():
    assert body_mod.bmi(160, None) is None
    assert body_mod.bmi(None, 50) is None


def test_length_note_is_a_ratio_not_a_guess():
    note = body_mod.length_note(160, 92)
    assert note["ratio"] == 0.575
    assert "腰线" in note["tip"]          # 明确说了不做位置猜测


# --------------------------------------------------------------------- 合身结论


def test_fit_report_recommends_a_size(charts):
    report = body_mod.fit_report(
        charts, {"bust": 80, "waist": 64, "height": 160, "weight": 50},
        brand="t", category="JSK",
    )
    assert report["status"] == "ok"
    assert report["ok"] is True
    assert report["recommended"] == "M"
    assert "能穿" in report["verdict"]
    assert report["length"]["ratio"] == pytest.approx(92 / 160, abs=0.001)
    assert report["bmi"]["band"] == "正常"


def test_fit_report_prefers_item_level_size_over_chart(charts):
    entry = {
        "id": "e1", "brand": "t", "brand_name": "T", "series": "测试区间裙",
        "items": ["JSK"],
        "size": {
            "kind": "free", "label": "F",
            "bust": [91, 101], "waist": [74, 86], "length": [89, 89],
            "source": "官方商品页",
        },
    }
    report = body_mod.fit_report(charts, {"bust": 84, "waist": 66}, entry=entry)
    assert report["status"] == "ok"
    assert report["recommended"] == "F"          # 不是品牌表的 M/L
    assert report["ranges"]["bust"] == [91, 101]
    assert report["verified"] is True
    assert "官方商品页" in report["source"]


def test_fit_report_item_level_too_small(charts):
    entry = {
        "id": "e1", "brand": "t", "brand_name": "T", "series": "测试区间裙",
        "items": ["JSK"],
        "size": {
            "kind": "free", "label": "F",
            "bust": [91, 101], "waist": [74, 86],
            "source": "官方商品页",
        },
    }
    report = body_mod.fit_report(charts, {"bust": 100}, entry=entry)
    assert report["status"] == "ok"
    assert report["ok"] is False
    assert report["recommended"] is None
    assert "穿不下" in report["verdict"]


def test_fit_report_without_profile_asks_for_measurements(charts):
    report = body_mod.fit_report(charts, {}, brand="t", category="JSK")
    assert report["status"] == "no_profile"
    assert report["ok"] is False
    assert "身材" in report["detail"]


def test_fit_report_without_chart_says_so(charts):
    report = body_mod.fit_report(charts, {"bust": 80}, brand="nope", category="JSK")
    assert report["status"] == "no_chart"
    assert "尺码表" in report["detail"]


def test_fit_report_partial_dimensions_lower_confidence(charts):
    """只有腰围也想判 JSK：引擎会按现有维度给码，但明确列出缺的那一维。"""
    report = body_mod.fit_report(charts, {"waist": 66}, brand="t", category="JSK")
    assert report["status"] == "ok"
    assert report["recommended"] is not None
    assert report["confidence"] == "medium"
    assert any("胸围" in item for item in report["advice"])


def test_fit_report_oversize_is_not_fit(charts):
    report = body_mod.fit_report(charts, {"bust": 120, "waist": 110}, brand="t", category="JSK")
    assert report["status"] == "ok"
    assert report["recommended"] is None
    assert report["ok"] is False
    assert "不建议" in report["verdict"] or "不够" in report["detail"]


def test_overrides_beat_stored_values(charts):
    """临时报的数字优先于存下来的：81+4=85 → 只有 L(88) 够。"""
    stored = {"bust": 81, "waist": 64}
    report = body_mod.fit_report(charts, stored, brand="t", category="JSK", overrides={"bust": 82})
    assert report["recommended"] == "L"


# --------------------------------------------------------------------- 落库


def test_save_and_get_roundtrip(ready_db):
    user, _token, _claimed = auth.register("body-db", "password123")
    saved = db.save_body(settings.db_path, user["id"], {"height": 160.0, "bust": 86.0})
    assert saved["height"] == 160.0
    assert db.get_body(settings.db_path, user["id"])["bust"] == 86.0


def test_partial_update_keeps_other_fields(ready_db):
    user, _token, _claimed = auth.register("body-db2", "password123")
    db.save_body(settings.db_path, user["id"], {"height": 160.0, "bust": 86.0})
    db.save_body(settings.db_path, user["id"], {"waist": 68.0})
    body = db.get_body(settings.db_path, user["id"])
    assert body["height"] == 160.0 and body["bust"] == 86.0 and body["waist"] == 68.0


def test_null_clears_one_field(ready_db):
    user, _token, _claimed = auth.register("body-db3", "password123")
    db.save_body(settings.db_path, user["id"], {"height": 160.0, "bust": 86.0})
    db.save_body(settings.db_path, user["id"], {"bust": None})
    body = db.get_body(settings.db_path, user["id"])
    assert "bust" not in body
    assert body["height"] == 160.0


def test_clearing_everything_removes_the_row(ready_db):
    """留一条"存过但什么都没有"的记录会让人以为存上了。"""
    user, _token, _claimed = auth.register("body-db4", "password123")
    db.save_body(settings.db_path, user["id"], {"height": 160.0})
    db.save_body(settings.db_path, user["id"], {"height": None})
    assert db.get_body(settings.db_path, user["id"]) == {}


def test_delete_user_removes_body(ready_db):
    user, _token, _claimed = auth.register("body-db5", "password123")
    db.save_body(settings.db_path, user["id"], {"bust": 86.0})
    assert db.delete_user(settings.db_path, user["id"]) is True
    assert db.get_body(settings.db_path, user["id"]) == {}


# --------------------------------------------------------------------- 接口


def test_body_requires_login(client):
    assert client.get("/api/profile/body").status_code == 401
    assert client.put("/api/profile/body", json={"bust": 86}).status_code == 401
    assert client.delete("/api/profile/body").status_code == 401


def test_put_and_get_body(client):
    headers = _auth_headers(client)
    response = client.put(
        "/api/profile/body",
        json={"height": 160, "weight": 50, "bust": 86, "waist": 68, "hip": 90, "shoulder": 38},
        headers=headers,
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["filled"] is True
    assert payload["bmi"]["band"] == "正常"
    assert payload["body"]["bust"] == 86.0
    assert "参与选码" in payload["note"]      # 说清体重不参与

    fetched = client.get("/api/profile/body", headers=headers).json()
    assert fetched["body"]["waist"] == 68.0


def test_put_out_of_range_is_400(client):
    headers = _auth_headers(client)
    response = client.put("/api/profile/body", json={"height": 1178}, headers=headers)
    assert response.status_code == 400
    assert "身高" in response.json()["detail"]


def test_put_is_partial(client):
    headers = _auth_headers(client)
    client.put("/api/profile/body", json={"height": 160, "bust": 86}, headers=headers)
    client.put("/api/profile/body", json={"waist": 68}, headers=headers)
    body = client.get("/api/profile/body", headers=headers).json()["body"]
    assert body["height"] == 160.0 and body["bust"] == 86.0 and body["waist"] == 68.0


def test_delete_clears_body(client):
    headers = _auth_headers(client)
    client.put("/api/profile/body", json={"bust": 86}, headers=headers)
    assert client.delete("/api/profile/body", headers=headers).json()["deleted"] is True
    assert client.get("/api/profile/body", headers=headers).json()["filled"] is False


def test_limits_are_exposed_for_the_form(client):
    headers = _auth_headers(client)
    limits = client.get("/api/profile/body", headers=headers).json()["limits"]
    assert limits["height"]["max"] == 220
    assert limits["weight"]["unit"] == "kg"
