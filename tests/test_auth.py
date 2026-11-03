"""认证测试。

这一层的错误代价最高（锁死账号或泄漏口令），所以逐条钉：
加盐、校验、吊销、过期、以及"用户名不存在"不能泄漏。
"""

from __future__ import annotations

import pytest

from app import auth, db


@pytest.fixture()
def db_ready(data_dir):
    from app.config import settings

    db.init_db(settings.db_path)
    return settings.db_path


# --------------------------------------------------------------------- 密码


def test_hash_is_salted(db_ready):
    first, salt_a = auth.hash_password("hunter22abc")
    second, salt_b = auth.hash_password("hunter22abc")
    assert salt_a != salt_b          # 每个账号独立盐，彩虹表无效
    assert first != second           # 同密码不同盐 → 不同哈希


def test_verify_roundtrip(db_ready):
    digest, salt = auth.hash_password("hunter22abc")
    assert auth.verify_password("hunter22abc", digest, salt) is True
    assert auth.verify_password("hunter22abd", digest, salt) is False


def test_verify_survives_bad_salt(db_ready):
    """损坏的盐不能让接口抛异常，只能判定为验证失败。"""
    assert auth.verify_password("hunter22abc", "deadbeef", "not-hex") is False


# --------------------------------------------------------------------- 注册


def test_register_creates_user_and_session(db_ready):
    user, token, claimed = auth.register("alice", "password123")
    assert user["username"] == "alice"
    assert claimed == 0
    assert db.session_user(db_ready, token)["username"] == "alice"


def test_duplicate_username_rejected(db_ready):
    auth.register("alice", "password123")
    with pytest.raises(auth.UsernameTaken):
        auth.register("alice", "password456")


@pytest.mark.parametrize("username, password, fragment", [
    ("ab", "password123", "3-32"),
    ("a" * 33, "password123", "3-32"),
    ("ali ce", "password123", "空格"),
    ("alice", "short", "至少 8 位"),
])
def test_validation_messages(db_ready, username, password, fragment):
    with pytest.raises(auth.AuthError) as exc:
        auth.register(username, password)
    assert fragment in str(exc.value)


def test_username_is_trimmed(db_ready):
    user, _, _ = auth.register("  alice  ", "password123")
    assert user["username"] == "alice"


# --------------------------------------------------------------------- 登录


def test_login_returns_fresh_token(db_ready):
    _, first_token, _ = auth.register("alice", "password123")
    _, second_token = auth.login("alice", "password123")
    assert first_token != second_token                    # 每次登录一个新会话
    assert db.session_user(db_ready, first_token) is not None
    assert db.session_user(db_ready, second_token) is not None


def test_login_wrong_password(db_ready):
    auth.register("alice", "password123")
    with pytest.raises(auth.InvalidCredentials):
        auth.login("alice", "password124")


def test_login_unknown_user_uses_same_error(db_ready):
    """不能让人从错误信息里区分"用户名不存在"和"密码错"。"""
    with pytest.raises(auth.InvalidCredentials) as unknown:
        auth.login("nobody", "password123")
    assert str(unknown.value) == "用户名或密码不对"


# --------------------------------------------------------------------- 会话


def test_logout_revokes_token(db_ready):
    _, token, _ = auth.register("alice", "password123")
    assert auth.logout(token) is True
    assert db.session_user(db_ready, token) is None


def test_unknown_token_is_none(db_ready):
    assert db.session_user(db_ready, "not-a-real-token") is None


def test_expired_session_is_rejected_and_cleaned(db_ready):
    user, _, _ = auth.register("alice", "password123")
    db.create_session(db_ready, "expired-token", int(user["id"]), "2000-01-01T00:00:00")
    assert db.session_user(db_ready, "expired-token") is None
    # 顺手删掉，避免表无限增长
    import sqlite3

    with sqlite3.connect(db_ready) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE token = 'expired-token'"
        ).fetchone()[0] == 0


# --------------------------------------------------------------------- 迁移


def test_first_registration_claims_legacy_follows(workdir, monkeypatch):
    """升级前遗留的"无归属店家"要归第一个注册的账号，不能让人发现偏好没了。

    这里把 ``data_dir`` 指到独立目录，而不是去删共享的 ``tests/.data/app.db``：
    手工清库会撞上其他用例留下的 ``-wal`` 句柄（Windows 上直接 WinError 32）。
    实测：单跑能过、跟别的用例连跑就红——所以隔离比清理更可靠。
    """
    import sqlite3

    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", workdir)
    db_path = settings.db_path

    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE follows (brand TEXT PRIMARY KEY, brand_name TEXT, created_at TEXT)")
    conn.execute("INSERT INTO follows VALUES ('sample-a', '示例牌 A', '2026-01-01T00:00:00')")
    conn.commit()
    conn.close()
    db.init_db(db_path)

    assert db.count_orphan_shops(db_path) == 1      # 还没人认领

    _, _, claimed = auth.register("alice", "password123")
    assert claimed == 1
    alice = db.get_user_by_name(db_path, "alice")
    assert db.get_preferences(db_path, int(alice["id"]))["shops"] == ["sample-a"]
    assert db.count_orphan_shops(db_path) == 0      # 认领后旧表删除

    _, _, second_claimed = auth.register("bob", "password123")
    assert second_claimed == 0
