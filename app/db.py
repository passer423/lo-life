"""用户数据层（SQLite）。

一套最小但**真实**的账号体系：users / sessions / preferences。

## 偏好为什么按"轴"存

`preferences(user_id, axis, values_json)` 一行一个轴，而不是一条记录塞几个列。
这样加一个新轴（价位带、颜色…）**不需要改表结构**——旧数据照样能读，
新轴天然为空。轴的定义在 `app/domain/axes.py`。

## 历史包袱（都在这一个函数里了）

* M1 的衣柜/出勤/清单三张表：直接删（当时确认过是空表）。
* M2 的 `follows`（无 user_id）：改名成 `follows_orphan` 保留，等第一个账号认领。
* M3 的 `follows`（有 user_id）+ `preferences(shops, styles)`：**合并进新轴模型**，
  因为"关注品牌"和"喜欢的店家"本来就是同一件事。

**合并顺序很重要**：先把旧数据读进内存，再把表删掉，最后建新表并写回。
反过来会在 Windows 上撞到 WAL 句柄，也会在出错时丢掉无法恢复的数据。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from app.domain.axes import AXES

SCHEMA_VERSION = 5

# 身材参数：**这是这个应用里最私人的几个数字**，所以单独一张表而不是塞进 preferences。
# 理由：偏好是公开标签（店家/风格），身材是身体数据；混在一起会让"导出偏好""打印偏好"
# 这类操作顺手把三围带走，也让将来想加密/单独删除时无从下手。
BODY_FIELDS = ("height", "weight", "bust", "waist", "hip", "shoulder")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    salt          TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS preferences (
    user_id     INTEGER NOT NULL,
    axis        TEXT NOT NULL,
    values_json TEXT NOT NULL DEFAULT '[]',
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (user_id, axis)
);

CREATE TABLE IF NOT EXISTS body_profile (
    user_id    INTEGER PRIMARY KEY,
    height     REAL,
    weight     REAL,
    bust       REAL,
    waist      REAL,
    hip        REAL,
    shoulder   REAL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

LEGACY_TABLES = ("items", "outfits", "wishlist")
ORPHAN_TABLE = "follows_orphan"


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@contextmanager
def connect(db_path: Path):
    """每次请求开一条连接：sqlite 打开很便宜，比维护连接池简单得多。

    ``WAL`` 让"读的同时在写"不会互相阻塞；手机和电脑同时开着页面就会用到。
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        yield conn
        conn.commit()
    finally:
        conn.close()


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name = ?", (table,)
        ).fetchone()[0]
    )


def init_db(db_path: Path) -> None:
    with connect(db_path) as conn:
        for table in LEGACY_TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {table}")

        # 1) 先把旧数据读进内存（此时什么都还没删）。
        #    用 dict 当**有序集合**：偏好本身无序，但升级时无故把用户选的顺序
        #    重排成字典序，会让人以为"我的设置被改了"。
        legacy_shops: dict[int, dict[str, None]] = {}
        legacy_styles: dict[int, dict[str, None]] = {}

        def _remember(bucket: dict[int, dict[str, None]], user_id: int, values) -> None:
            target = bucket.setdefault(user_id, {})
            for value in values:
                target.setdefault(str(value), None)

        pref_columns = _columns(conn, "preferences")
        if pref_columns and "axis" not in pref_columns:
            for row in conn.execute("SELECT user_id, shops, styles FROM preferences").fetchall():
                user_id = int(row["user_id"])
                _remember(legacy_shops, user_id, _loads(row["shops"]))
                _remember(legacy_styles, user_id, _loads(row["styles"]))

        follow_columns = _columns(conn, "follows")
        if follow_columns and "user_id" not in follow_columns:
            # M2 的旧表没有 user_id：改名保留，等第一个注册的账号认领
            conn.execute(f"ALTER TABLE follows RENAME TO {ORPHAN_TABLE}")
        elif follow_columns:
            # M3 的 follows 有 user_id：合并进 shops 轴，然后删表
            for row in conn.execute("SELECT user_id, brand FROM follows").fetchall():
                _remember(legacy_shops, int(row["user_id"]), [row["brand"]])
            conn.execute("DROP TABLE follows")

        # 2) 旧 preferences 结构不兼容：删掉（数据已在上面读出来了）
        if pref_columns and "axis" not in pref_columns:
            conn.execute("DROP TABLE preferences")

        # 3) 建新表
        conn.executescript(SCHEMA)

        # 4) 旧数据写回新结构
        for user_id, shops in legacy_shops.items():
            if shops:
                _upsert_axis(conn, user_id, "shops", list(shops))
        for user_id, styles in legacy_styles.items():
            if styles:
                _upsert_axis(conn, user_id, "styles", list(styles))

        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )


def _dicts(rows) -> list[dict]:
    return [dict(row) for row in rows]


# --------------------------------------------------------------------- 账号


def create_user(db_path: Path, username: str, password_hash: str, salt: str) -> dict | None:
    """返回新用户；用户名已存在时返回 None（交给调用方转成 409）。"""
    with connect(db_path) as conn:
        try:
            cursor = conn.execute(
                "INSERT INTO users (username, password_hash, salt, created_at) VALUES (?, ?, ?, ?)",
                (username, password_hash, salt, now()),
            )
        except sqlite3.IntegrityError:
            return None
        row = conn.execute("SELECT * FROM users WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return dict(row) if row else None


def get_user_by_name(db_path: Path, username: str) -> dict | None:
    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    return dict(row) if row else None


def count_users(db_path: Path) -> int:
    with connect(db_path) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])


def delete_user(db_path: Path, user_id: int) -> bool:
    """彻底删掉一个账号及其全部数据（会话、偏好）。

    注销必须是**真的删**，不是打个标记：这台机器可能转给别人用，
    或者你只是想从头来一遍。没有软删除的分支要维护，也就没有"其实还在"的惊讶。
    """
    with connect(db_path) as conn:
        exists = conn.execute("SELECT COUNT(*) FROM users WHERE id = ?", (user_id,)).fetchone()[0]
        if not exists:
            return False
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM preferences WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM body_profile WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        return True


# --------------------------------------------------------------------- 会话


def create_session(db_path: Path, token: str, user_id: int, expires_at: str) -> None:
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (token, user_id, now(), expires_at),
        )


def session_user(db_path: Path, token: str) -> dict | None:
    """令牌 → 用户。过期令牌会被顺手删掉（不然表会无限长）。"""
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT u.id, u.username, u.created_at, s.expires_at "
            "FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token = ?",
            (token,),
        ).fetchone()
        if row is None:
            return None
        if row["expires_at"] <= now():
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            return None
    return {"id": row["id"], "username": row["username"], "created_at": row["created_at"]}


def delete_session(db_path: Path, token: str) -> bool:
    with connect(db_path) as conn:
        return conn.execute("DELETE FROM sessions WHERE token = ?", (token,)).rowcount > 0


def delete_expired_sessions(db_path: Path) -> int:
    with connect(db_path) as conn:
        return conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now(),)).rowcount


# --------------------------------------------------------------------- 偏好


def get_preferences(db_path: Path, user_id: int) -> dict[str, list[str]]:
    """返回**所有**轴（没设过的为空列表），省得调用方到处写 ``or []``。"""
    data: dict[str, list[str]] = {axis.key: [] for axis in AXES}
    with connect(db_path) as conn:
        for row in conn.execute(
            "SELECT axis, values_json FROM preferences WHERE user_id = ?", (user_id,)
        ).fetchall():
            if row["axis"] in data:
                data[row["axis"]] = _loads(row["values_json"])
    return data


def preferences_updated_at(db_path: Path, user_id: int) -> str:
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT MAX(updated_at) FROM preferences WHERE user_id = ?", (user_id,)
        ).fetchone()
    return str(row[0] or "") if row else ""


def save_preferences(db_path: Path, user_id: int, axes: dict[str, list[str]]) -> dict[str, list[str]]:
    """只替换**传进来的轴**，没传的保持不变。

    这样前端可以一块一块地保存（改风格不动店家），也避免"漏传一个字段
    就把其他偏好清空"这类最容易让人暴怒的 bug。
    """
    with connect(db_path) as conn:
        for key, values in axes.items():
            if values:
                _upsert_axis(conn, user_id, key, values)
            else:
                conn.execute(
                    "DELETE FROM preferences WHERE user_id = ? AND axis = ?", (user_id, key)
                )
    return get_preferences(db_path, user_id)


def _upsert_axis(conn: sqlite3.Connection, user_id: int, axis: str, values: list[str]) -> None:
    conn.execute(
        "INSERT INTO preferences (user_id, axis, values_json, updated_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(user_id, axis) DO UPDATE SET values_json = excluded.values_json, "
        "updated_at = excluded.updated_at",
        (user_id, axis, json.dumps(values, ensure_ascii=False), now()),
    )


def _loads(raw: str) -> list[str]:
    try:
        parsed = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


# --------------------------------------------------------------------- 旧关注认领


def count_orphan_shops(db_path: Path) -> int:
    """M2 遗留的"没有归属的店家列表"条数（旧表没有 user_id）。"""
    with connect(db_path) as conn:
        if not _table_exists(conn, ORPHAN_TABLE):
            return 0
        return int(conn.execute(f"SELECT COUNT(*) FROM {ORPHAN_TABLE}").fetchone()[0])


def claim_orphan_shops(db_path: Path, user_id: int) -> int:
    """把这些遗留店家并进该账号的 shops 轴（只在注册时调用），然后删掉旧表。

    这是**迁移**而不是业务：旧表没有 user_id，谁认领都只能是"第一个注册的人"。
    认领后立即删表，避免第二个账号也来认领同一批数据。
    """
    with connect(db_path) as conn:
        if not _table_exists(conn, ORPHAN_TABLE):
            return 0
        brands = [str(row[0]) for row in conn.execute(f"SELECT brand FROM {ORPHAN_TABLE}").fetchall()]
        # 用**同一条连接**读已有 shops：WAL 下再开一条连接去读，容易和写事务互相等
        row = conn.execute(
            "SELECT values_json FROM preferences WHERE user_id = ? AND axis = 'shops'", (user_id,)
        ).fetchone()
        existing = _loads(row["values_json"]) if row else []
        # 保留已有顺序，认领来的追加在后面
        merged = list(dict.fromkeys([*existing, *brands]))
        if merged:
            _upsert_axis(conn, user_id, "shops", merged)
        conn.execute(f"DROP TABLE {ORPHAN_TABLE}")
        return len(brands)


# --------------------------------------------------------------------- 身材


def get_body(db_path: Path, user_id: int) -> dict:
    """已保存的身材参数；没有时返回空 dict（不是 ``None``，调用方少一层判断）。"""
    with connect(db_path) as conn:
        row = conn.execute(
            f"SELECT {', '.join(BODY_FIELDS)}, updated_at FROM body_profile WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    if row is None:
        return {}
    data = {field: row[field] for field in BODY_FIELDS if row[field] is not None}
    if data:
        data["updated_at"] = row["updated_at"]
    return data


def save_body(db_path: Path, user_id: int, values: dict) -> dict:
    """部分更新，语义与偏好 PUT 一致。

    * 只有传进来的字段会被改；
    * 显式传 ``None`` 表示**清空该项**——这是删掉单个数字的唯一方式；
    * 全部为空时直接删行：留一条"存过但什么都没有"的记录会让人以为存上了。
    """
    current = get_body(db_path, user_id)
    merged: dict[str, float | None] = {field: current.get(field) for field in BODY_FIELDS}
    for field in BODY_FIELDS:
        if field in values:
            merged[field] = values[field]

    with connect(db_path) as conn:
        conn.execute("DELETE FROM body_profile WHERE user_id = ?", (user_id,))
        if any(value is not None for value in merged.values()):
            columns = ", ".join(("user_id", *BODY_FIELDS, "updated_at"))
            marks = ", ".join(["?"] * (len(BODY_FIELDS) + 2))
            conn.execute(
                f"INSERT INTO body_profile ({columns}) VALUES ({marks})",
                (user_id, *(merged[field] for field in BODY_FIELDS), now()),
            )
    return get_body(db_path, user_id)


def delete_body(db_path: Path, user_id: int) -> bool:
    with connect(db_path) as conn:
        cursor = conn.execute("DELETE FROM body_profile WHERE user_id = ?", (user_id,))
        return cursor.rowcount > 0
