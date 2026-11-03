"""注册 / 登录 / 退出 / 当前用户。

薄壳：所有密码与令牌的活都在 app/auth.py 里，这里只做 HTTP 语义翻译。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app import auth

router = APIRouter(prefix="/api/auth", tags=["auth"])


class CredentialsIn(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=200)


@router.post("/register", status_code=201)
def register(payload: CredentialsIn) -> dict:
    try:
        user, token, claimed = auth.register(payload.username, payload.password)
    except auth.UsernameTaken as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except auth.AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # claimed_follows > 0 说明这是升级后第一个注册的账号，接手了旧版留下的关注
    return {"user": user, "token": token, "claimed_follows": claimed}


@router.post("/login")
def login(payload: CredentialsIn) -> dict:
    try:
        user, token = auth.login(payload.username, payload.password)
    except auth.InvalidCredentials as exc:
        # 401 而不是 404：不能告诉猜密码的人"这个用户名存在但密码错"
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    return {"user": user, "token": token}


@router.post("/logout")
def logout(request: Request) -> dict:
    token = auth.bearer_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="没有登录")
    return {"logged_out": auth.logout(token)}


@router.get("/me")
def me(user: dict | None = Depends(auth.optional_user)) -> dict:
    """未登录也返回 200 + ``user: null``：前端只需判断 user 是否为空，少一个分支。"""
    return {"user": user}


@router.delete("/me")
def delete_account(user: dict = Depends(auth.require_user)) -> dict:
    """注销账号：连同会话、关注、偏好一起删掉，不留软删除标记。"""
    from app import db
    from app.config import settings

    if not db.delete_user(settings.db_path, int(user["id"])):
        raise HTTPException(status_code=404, detail="账号不存在")
    return {"deleted": user["username"]}
