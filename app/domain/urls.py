"""出口链接的白名单。

## 为什么必须在后端收一次

`link` / `image` 来自 **手录 JSON**，而这份 JSON 可能来自：

* 自己从店铺页复制（还可能手抖粘错）；
* 备份 zip 的恢复（`POST /api/import` 那条路径）；
* 别人给的条目文件。

前端 `esc()` 只转义 HTML 实体，**不校验 scheme**——`javascript:alert(1)`
放进 ``href`` 里照样会执行，`data:text/html,...` 放进 ``img src`` 同理。
所以校验放在序列化出口：坏链接直接变成空串，前端据此不渲染按钮/不使用图片。

## 放行范围刻意很小

只有三种：

1. ``https://`` / ``http://`` 的绝对地址（店铺页、商品页）；
2. 以 ``/media/`` 开头的站内路径（`POST /api/media` 上传后的地址）；
3. 空串（表示"没有"，不是错误）。

其余一律拒绝——包括 ``javascript:``、``data:``、协议相对地址 ``//evil.com``
和带 ``..`` 的路径穿越。宁可少显示一个按钮，也不让一个手抖的粘贴变成注入点。
"""

from __future__ import annotations

_SCHEMES = ("http://", "https://")

_MEDIA_PREFIX = "/media/"


def safe_url(value: str, *, allow_media: bool = True) -> str:
    """返回可安全放进 href/src 的地址；不合格返回空串（**不是** None，前端好判断）。"""
    text = str(value or "").strip()
    if not text:
        return ""

    # 控制字符和空白一律拒绝：URL 里不该有空格，而换行常被用来拼接响应头。
    if any(ch.isspace() or ord(ch) < 0x20 for ch in text):
        return ""

    lowered = text.lower()
    if any(lowered.startswith(scheme) for scheme in _SCHEMES):
        return text

    if allow_media and lowered.startswith(_MEDIA_PREFIX):
        # 站内路径只允许 /media/ 下的普通文件名：不认 .. ，不认 //（会变成协议相对地址）
        remainder = text[len(_MEDIA_PREFIX):]
        if not remainder or ".." in remainder or "//" in remainder:
            return ""
        return text

    return ""
