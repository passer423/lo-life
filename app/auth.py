"""账号与登录态。

## 只做必要的，但每一步都做对

* **密码**用 ``hashlib.scrypt``（stdlib，无反例库依赖）+ 每用户独立盐；
* **登录态**是随机令牌存库，客户端用 ``Authorization: Bearer`` 发送。
  没用 JWT：本地服务里"能立刻吊销"比"无状态"重要——点退出登录必须真的失效；
* **用户名不存在时也算一次哈希**，避免用响应时间探测哪些用户名存在。

## 刻意没做的

邮箱验证、找回密码、登录失败锁定、多因素。这些都需要外部服务或持久化运维，
而当前是单机自用场景。**代价要清楚**：忘了密码等于丢了账号数据，
所以密码请记在密码管理器里；明文 HTTP 下令牌可被同网段嗅探，
在局域网里用请走 Tailscale（见 README）。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta

from fastapi import Depends, HTTPException, Request

from app import db
from app.config import settings

USERNAME_MIN, USERNAME_MAX = 3, 32
PASSWORD_MIN = 8
SESSION_DAYS = 30

# scrypt 参数：N=16384 在普通 CPU 上约 100ms，足够挡住离线爆破的性价比
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P, _DKLEN = 16384, 8, 1, 32


class AuthError(Exception):
    """认证类错误。由路由翻译成 4xx。"""


class UsernameTaken(AuthError):
    pass


class InvalidCredentials(AuthError):
    pass


# --------------------------------------------------------------------- 密码


def hash_password(password: str, salt_hex: str | None = None) -> tuple[str, str]:
    salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_DKLEN
    )
    return digest.hex(), salt.hex()


def verify_password(password: str, password_hash: str, salt_hex: str) -> bool:
    try:
        candidate, _ = hash_password(password, salt_hex)
    except ValueError:
        return False
    return hmac.compare_digest(candidate, password_hash)


# 用户名不存在时也跑一次哈希，让"不存在"和"密码错"耗时相同
_DUMMY_HASH, _DUMMY_SALT = hash_password("lo-life-dummy-password")


# --------------------------------------------------------------------- 校验


def normalize_username(username: str) -> str:
    return (username or "").strip()


def validate_credentials(username: str, password: str) -> str:
    """返回错误信息；空字符串表示通过。"""
    if not (USERNAME_MIN <= len(username) <= USERNAME_MAX):
        return f"用户名需要 {USERNAME_MIN}-{USERNAME_MAX} 个字符"
    if any(char.isspace() for char in username):
        return "用户名不能包含空格"
    if len(password or "") < PASSWORD_MIN:
        return f"密码至少 {PASSWORD_MIN} 位"
    return ""


# --------------------------------------------------------------------- 会话


def issue_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    expires = (datetime.now() + timedelta(days=SESSION_DAYS)).isoformat(timespec="seconds")
    db.create_session(settings.db_path, token, user_id, expires)
    return token


def bearer_token(request: Request) -> str:
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return ""


def optional_user(request: Request) -> dict | None:
    """当前用户；未登录返回 None（浏览类接口都用它，不需要强制登录）。"""
    token = bearer_token(request)
    if not token:
        return None
    return db.session_user(settings.db_path, token)


def require_user(user: dict | None = Depends(optional_user)) -> dict:
    """需要登录的接口用它。返回 401 时前端会清掉本地令牌。"""
    if user is None:
        raise HTTPException(
            status_code=401,
            detail="需要登录后才能使用这个功能",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


# --------------------------------------------------------------------- 注册 / 登录


def register(username: str, password: str) -> tuple[dict, str, int]:
    """返回 (用户, 令牌, 认领的旧关注条数)。"""
    username = normalize_username(username)
    error = validate_credentials(username, password)
    if error:
        raise AuthError(error)

    password_hash, salt = hash_password(password)
    user = db.create_user(settings.db_path, username, password_hash, salt)
    if user is None:
        raise UsernameTaken(f"用户名「{username}」已经被注册了")

    # 升级前遗留的"无归属店家"（旧 follows 表没有 user_id）并进第一个账号的 shops 轴
    claimed = db.claim_orphan_shops(settings.db_path, int(user["id"]))
    token = issue_session(int(user["id"]))
    return {"id": user["id"], "username": user["username"]}, token, claimed


def login(username: str, password: str) -> tuple[dict, str]:
    username = normalize_username(username)
    user = db.get_user_by_name(settings.db_path, username)
    if user is None:
        # 走一遍等价的哈希，避免"响应快 = 用户名不存在"这个信息泄漏
        verify_password(password, _DUMMY_HASH, _DUMMY_SALT)
        raise InvalidCredentials("用户名或密码不对")

    if not verify_password(password, user["password_hash"], user["salt"]):
        raise InvalidCredentials("用户名或密码不对")

    token = issue_session(int(user["id"]))
    return {"id": user["id"], "username": user["username"]}, token


def logout(token: str) -> bool:
    return db.delete_session(settings.db_path, token) if token else False
