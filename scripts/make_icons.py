"""生成 PWA 图标（192 / 512 PNG）。

放在 scripts/ 而不是手工丢两张图进去，是因为「图标缺了导致装不上」这类问题
很难被察觉：Android 只会在控制台里默默不给安装提示。能重新生成就永远不缺。

用法：python scripts/make_icons.py
"""

from __future__ import annotations

import sys
from pathlib import Path

TARGET = Path(__file__).resolve().parents[1] / "app" / "static" / "icons"
BG = (184, 102, 122)        # 与 style.css 的 --accent 一致
FG = (255, 253, 251)

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\seguisb.ttf",
    r"C:\Windows\Fonts\arialbd.ttf",
]


def _font(size: int):
    from PIL import ImageFont

    for path in FONT_CANDIDATES:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def render(size: int, *, maskable: bool) -> object:
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (size, size), BG + (255,))
    draw = ImageDraw.Draw(image)

    # maskable 图标会被裁掉外圈，所以内容收进中间 60%
    ratio = 0.42 if maskable else 0.56
    font = _font(int(size * ratio))
    text = "lo"
    box = draw.textbbox((0, 0), text, font=font)
    draw.text(
        ((size - (box[2] - box[0])) / 2 - box[0], (size - (box[3] - box[1])) / 2 - box[1]),
        text,
        font=font,
        fill=FG + (255,),
    )

    # 一圈细边：纯色方块在主屏上会显得比别的图标大
    inset = int(size * 0.06)
    draw.ellipse(
        (inset, inset, size - inset, size - inset),
        outline=FG + (70,),
        width=max(2, int(size * 0.012)),
    )
    return image


def main() -> int:
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        print("缺少 Pillow。安装：python -m pip install pillow", file=sys.stderr)
        return 1

    TARGET.mkdir(parents=True, exist_ok=True)
    for size in (192, 512):
        path = TARGET / f"icon-{size}.png"
        render(size, maskable=False).save(path, "PNG")
        print(f"已生成 {path}（{path.stat().st_size} 字节）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
