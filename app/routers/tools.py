"""确定性工具路由：尺码推荐、洗护建议。

两个路由都**每次请求现读 JSON**（文件只有几 KB）：改完 `content/size/*.json`
或 `content/care/rules.json` 立刻生效，不需要重启服务。对"边用边补数据"的场景，
这一点比缓存那几百微秒重要得多。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.config import settings
from app.domain import care, size

router = APIRouter(prefix="/api", tags=["tools"])


# --------------------------------------------------------------------- 尺码


class SizeIn(BaseModel):
    brand: str = Field(..., min_length=1, description="content/size 里的品牌 id")
    category: str = Field("JSK", description="JSK / OP / SK 等")
    # 只收真实身体尺寸。刻意不收「身高体重」：没有体型模型时，仅凭身高体重推不出
    # 胸围腰围，硬给一个数字就是伪精确。
    bust: float | None = Field(None, gt=0, le=200, description="胸围 cm")
    waist: float | None = Field(None, gt=0, le=200, description="腰围 cm")
    hip: float | None = Field(None, gt=0, le=200, description="臀围 cm")
    shoulder: float | None = Field(None, gt=0, le=100, description="肩宽 cm")


@router.get("/size/brands")
def size_brands() -> dict:
    try:
        charts = size.load_charts(settings.size_dir)
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {
        "brands": size.list_brands(charts),
        "dimensions": list(size.dimensions_of(charts)),
        "note": (
            "AP/Baby 的 kind=item_level：官方只给商品级实测尺寸，不提供品牌级 S/M/L 表。"
            "品牌级尺码表请按 content/size/_README.md 录入，且必须有官方来源。"
        ),
    }


@router.post("/size/recommend")
def size_recommend(payload: SizeIn) -> dict:
    try:
        charts = size.load_charts(settings.size_dir)
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    measurements = {
        key: getattr(payload, key)
        for key in ("bust", "waist", "hip", "shoulder")
        if getattr(payload, key) is not None
    }
    try:
        return size.recommend(charts, payload.brand, payload.category, measurements)
    except size.ChartNotFound as exc:
        # 404 而不是 500：这是"我们没有这份数据"，不是"服务坏了"
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# --------------------------------------------------------------------- 洗护


class CareIn(BaseModel):
    materials: list[str] = Field(default_factory=list)
    crafts: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


@router.get("/care/options")
def care_options() -> dict:
    try:
        _, options = care.load_rules(settings.care_dir)
    except care.RulesUnavailable as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return options


@router.post("/care/advice")
def care_advice(payload: CareIn) -> dict:
    try:
        rules, _ = care.load_rules(settings.care_dir)
    except care.RulesUnavailable as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    try:
        return care.advise(
            rules,
            materials=payload.materials,
            crafts=payload.crafts,
            notes=payload.notes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
