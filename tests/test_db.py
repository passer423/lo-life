"""数据层测试：偏好轴、账号、以及三处历史迁移。

偏好按轴存的意义在于**加轴不改表**，所以这里既要测"按轴读写"，也要测
"只改一个轴不会动其他轴"——那是用户最容易暴怒的一类 bug。

迁移部分单独测，因为它们是**破坏性**的：旧表要么合并、要么改名保留，
绝不能不声不响地丢掉。
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from app import db

ALICE = 1
BOB = 2


@pytest.fixture()
def path(workdir):
    db_path = workdir / "app.db"
    db.init_db(db_path)
    return db_path


def test_schema_version_is_recorded(path):
    with sqlite3.connect(path) as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    assert int(row[0]) == db.SCHEMA_VERSION


# --------------------------------------------------------------------- 偏好轴


def test_all_axes_present_and_empty_by_default(path):
    prefs = db.get_preferences(path, ALICE)
    assert set(prefs) == {"shops", "styles", "keywords", "categories"}
    assert all(values == [] for values in prefs.values())


def test_save_and_read_back(path):
    db.save_preferences(path, ALICE, {"shops": ["sample-a"], "styles": ["甜系"]})
    prefs = db.get_preferences(path, ALICE)
    assert prefs["shops"] == ["sample-a"]
    assert prefs["styles"] == ["甜系"]
    assert prefs["categories"] == []


def test_partial_update_keeps_other_axes(path):
    """只保存一个轴时，其他轴必须原样保留——否则前端分块保存就会互相清空。"""
    db.save_preferences(path, ALICE, {"shops": ["sample-a"], "styles": ["甜系"]})
    db.save_preferences(path, ALICE, {"categories": ["JSK"]})
    prefs = db.get_preferences(path, ALICE)
    assert prefs["shops"] == ["sample-a"]
    assert prefs["styles"] == ["甜系"]
    assert prefs["categories"] == ["JSK"]


def test_empty_list_clears_that_axis_only(path):
    db.save_preferences(path, ALICE, {"shops": ["sample-a"], "styles": ["甜系"]})
    db.save_preferences(path, ALICE, {"shops": []})
    prefs = db.get_preferences(path, ALICE)
    assert prefs["shops"] == []
    assert prefs["styles"] == ["甜系"]


def test_preferences_are_isolated_per_user(path):
    db.save_preferences(path, ALICE, {"shops": ["sample-a"]})
    assert db.get_preferences(path, BOB)["shops"] == []


def test_updated_at_is_reported(path):
    assert db.preferences_updated_at(path, ALICE) == ""
    db.save_preferences(path, ALICE, {"shops": ["sample-a"]})
    assert db.preferences_updated_at(path, ALICE)


def test_broken_json_does_not_crash(path):
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO preferences (user_id, axis, values_json, updated_at) VALUES (?, ?, ?, ?)",
            (ALICE, "shops", "{not json", "2026-01-01T00:00:00"),
        )
    assert db.get_preferences(path, ALICE)["shops"] == []


# --------------------------------------------------------------------- 账号


def test_delete_user_removes_preferences(path):
    user = db.create_user(path, "alice", "hash", "salt")
    db.save_preferences(path, int(user["id"]), {"shops": ["sample-a"]})
    assert db.delete_user(path, int(user["id"])) is True
    assert db.count_users(path) == 0
    assert db.get_preferences(path, int(user["id"]))["shops"] == []


def test_delete_missing_user_is_false(path):
    assert db.delete_user(path, 999) is False


# --------------------------------------------------------------------- 迁移


def test_legacy_item_tables_are_dropped(workdir):
    db_path = workdir / "app.db"
    conn = sqlite3.connect(db_path)
    for table in db.LEGACY_TABLES:
        conn.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
        conn.execute(f"INSERT INTO {table} DEFAULT VALUES")
    conn.commit()
    conn.close()

    db.init_db(db_path)

    with sqlite3.connect(db_path) as conn:
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in db.LEGACY_TABLES:
        assert table not in names
    assert "preferences" in names and "users" in names


def test_legacy_preferences_columns_become_axes(workdir):
    """M3 的 preferences 是 (user_id, shops, styles) 单行多列 —— 要搬进轴模型。"""
    db_path = workdir / "app.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE preferences (user_id INTEGER PRIMARY KEY, shops TEXT, styles TEXT, updated_at TEXT)"
    )
    conn.execute(
        "INSERT INTO preferences VALUES (?, ?, ?, ?)",
        (ALICE, json.dumps(["sample-a"]), json.dumps(["甜系", "古典"]), "2026-01-01T00:00:00"),
    )
    conn.commit()
    conn.close()

    db.init_db(db_path)

    prefs = db.get_preferences(db_path, ALICE)
    assert prefs["shops"] == ["sample-a"]
    assert prefs["styles"] == ["甜系", "古典"]
    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(preferences)")}
    assert "axis" in columns


def test_legacy_follows_are_merged_into_shops(workdir):
    """M3 的 follows(user_id, brand) 与"喜欢的店家"是同一件事：合并后删表。"""
    db_path = workdir / "app.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE follows (user_id INTEGER, brand TEXT, brand_name TEXT, created_at TEXT, "
        "PRIMARY KEY (user_id, brand))"
    )
    conn.execute("INSERT INTO follows VALUES (?, 'sample-b', '示例牌 B', '2026-01-01T00:00:00')", (ALICE,))
    conn.commit()
    conn.close()

    db.init_db(db_path)

    assert db.get_preferences(db_path, ALICE)["shops"] == ["sample-b"]
    with sqlite3.connect(db_path) as conn:
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "follows" not in names


def test_orphan_follows_are_renamed_not_dropped(workdir):
    """M2 的 follows 没有 user_id：改名保留，等第一个账号认领，不能静默删掉。"""
    db_path = workdir / "app.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE follows (brand TEXT PRIMARY KEY, brand_name TEXT, created_at TEXT)")
    conn.execute("INSERT INTO follows VALUES ('sample-a', '示例牌 A', '2026-01-01T00:00:00')")
    conn.commit()
    conn.close()

    db.init_db(db_path)

    assert db.count_orphan_shops(db_path) == 1
    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(preferences)")}
    assert "axis" in columns


def test_claim_orphan_shops_is_one_shot(workdir):
    db_path = workdir / "app.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE follows (brand TEXT PRIMARY KEY, brand_name TEXT, created_at TEXT)")
    conn.execute("INSERT INTO follows VALUES ('sample-a', '示例牌 A', '2026-01-01T00:00:00')")
    conn.commit()
    conn.close()
    db.init_db(db_path)

    assert db.claim_orphan_shops(db_path, ALICE) == 1
    assert db.get_preferences(db_path, ALICE)["shops"] == ["sample-a"]
    assert db.claim_orphan_shops(db_path, BOB) == 0        # 第二个账号拿不到
    assert db.get_preferences(db_path, BOB)["shops"] == []


def test_claim_merges_with_existing_shops(workdir):
    """已经有偏好店家的账号认领时，应该是**合并**，不能覆盖掉自己选的。"""
    db_path = workdir / "app.db"
    db.init_db(db_path)
    db.save_preferences(db_path, ALICE, {"shops": ["sample-b"]})

    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE follows_orphan (brand TEXT PRIMARY KEY, brand_name TEXT, created_at TEXT)")
        conn.execute("INSERT INTO follows_orphan VALUES ('sample-a', '示例牌 A', '2026-01-01T00:00:00')")

    assert db.claim_orphan_shops(db_path, ALICE) == 1
    # 已有顺序保留，认领来的追加在后面
    assert db.get_preferences(db_path, ALICE)["shops"] == ["sample-b", "sample-a"]


def test_init_is_repeatable(path):
    db.save_preferences(path, ALICE, {"shops": ["sample-a"], "styles": ["甜系"]})
    db.init_db(path)                    # 再跑一次不能把数据清掉
    prefs = db.get_preferences(path, ALICE)
    assert prefs["shops"] == ["sample-a"]
    assert prefs["styles"] == ["甜系"]
