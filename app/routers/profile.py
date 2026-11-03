"""身材参数接口。

## 隐私处理（这是这个应用里最私人的几个数字）

* **必须登录**，匿名一律 401——没有"游客也能试试"的分支。
* 走 PUT body，**绝不进 URL**：URL 会进浏览器历史、访问日志、以及 Referer 头。
  这也是 ``/api/feed/sizes`` 当初就只用 POST body 的原因。
* **不写日志**：这里没有任何 ``logger.info(payload)``，报错文案里也不回显数值。
* 单独一张表，注销账号时随 ``db.delete_user`` 一起删；另有 DELETE 接口可单独清空。

## 为什么要存（这推翻了早先"三围不落库"的决定）

早先的目标是"算完就丢"，因为当时它只服务一次性的"按下算码"。现在它要给助手用：
用户问「这条我穿得上吗」时，模型不该反过来问三围——那既不礼貌也不必要。
存下来的前提是把上面的隔离做对，而不是含糊过去。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app import auth, db
from app.config import settings
from app.domain import body as body_mod

router = APIRouter(prefix="/api/profile", tags=["profile"])


class BodyIn(BaseModel):
    """只处理**显式传的字段**：不传=不改，传 null=清空，传数字=更新。"""

    height: float | None = None
    weight: float | None = None
    bust: float | None = None
    waist: float | None = None
    hip: float | None = None
    shoulder: float | None = None


def _limits() -> dict:
    return {
        field: {"label": label, "min": low, "max": high, "unit": body_mod.UNIT[field]}
        for field, (low, high, label) in body_mod.LIMITS.items()
    }


def _payload(user: dict, saved: dict) -> dict:
    return {
        "body": saved,
        "filled": bool(saved),
        "bmi": body_mod.bmi(saved.get("height"), saved.get("weight")),
        "limits": _limits(),
        # 说清楚哪些数字参与选码、哪些不参与，免得用户以为填了体重就能算码
        "note": "尺码表按围度判断合身；体重只用于 BMI 提示，不参与选码。",
    }


@router.get("/body")
def get_body(user: dict = Depends(auth.require_user)) -> dict:
    return _payload(user, db.get_body(settings.db_path, int(user["id"])))


@router.put("/body")
def put_body(payload: BodyIn, user: dict = Depends(auth.require_user)) -> dict:
    raw = payload.model_dump(exclude_unset=True)
    clean, problems = body_mod.sanitize(raw)
    if problems:
        raise HTTPException(status_code=400, detail="；".join(problems))
    saved = db.save_body(settings.db_path, int(user["id"]), clean)
    return _payload(user, saved)


@router.delete("/body")
def delete_body(user: dict = Depends(auth.require_user)) -> dict:
    deleted = db.delete_body(settings.db_path, int(user["id"]))
    return {"deleted": deleted, "body": {}}
