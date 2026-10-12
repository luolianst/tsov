"""卷帘 PNG 渲染（批F F3-2）：服务端 PIL 画钢琴卷帘——给外部 agent「装眼睛」。

不依赖 CDP / 浏览器；轻量版：轨道行 × 时间轴 × 音符矩形 + 行标签 + 小节线。
"""

from __future__ import annotations

import io
import os

from PIL import Image, ImageDraw, ImageFont

_PALETTE = [
    (120, 170, 255), (255, 150, 110), (120, 220, 160), (230, 200, 90),
    (200, 140, 255), (110, 220, 230), (240, 130, 180), (170, 210, 120),
]


def _font(size: int = 13):
    winfonts = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
    for name in ("msyh.ttc", "simhei.ttf", "arial.ttf", "segoeui.ttf"):
        try:
            return ImageFont.truetype(os.path.join(winfonts, name), size)
        except Exception:  # noqa: BLE001 字体缺失逐个回退
            continue
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def render_roll_png(proj, *, width: int = 1600, row_h: int = 34, name_w: int = 150) -> bytes:
    """渲染工程卷帘为 PNG 字节（确定性；无副作用）。"""
    score = proj.score
    tracks = list(getattr(score, "tracks", []) or [])

    end = 1.0
    pms: list[int] = []
    for t in tracks:
        for n in t.notes:
            end = max(end, float(n.end))
            pms.append(int(n.pitch_midi))
    lo, hi = (min(pms), max(pms)) if pms else (48, 72)
    if hi - lo < 12:
        mid = (lo + hi) // 2
        lo, hi = mid - 6, mid + 6

    rows = max(1, len(tracks))
    head_h = 46
    img = Image.new("RGB", (width, head_h + rows * row_h + 8), (22, 22, 26))
    d = ImageDraw.Draw(img)
    f = _font(13)
    f2 = _font(12)

    def _text(xy, s, fill, font):
        try:
            d.text(xy, s, fill=fill, font=font)
        except Exception:  # noqa: BLE001 字形壁障不炸渲染
            pass

    _text((10, 8), f"{proj.name}  tempo={score.tempo}  tracks={len(tracks)}  span={end:.1f}s", (230, 230, 235), f)
    _text((10, 26), "服务端卷帘渲染（tsov MCP · 实验性）", (140, 140, 150), f2)

    plot_x0 = name_w + 10
    plot_w = max(10, width - plot_x0 - 10)

    for i, t in enumerate(tracks):
        y0 = head_h + i * row_h
        if i % 2:
            d.rectangle([0, y0, width, y0 + row_h], fill=(28, 28, 33))
        kind = str(getattr(t, "kind", "midi") or "midi")
        _text((8, y0 + row_h // 2 - 8), f"{i}:{t.name}"[:16], (170, 170, 180), f2)
        if kind == "audio":
            d.rectangle([plot_x0, y0 + 10, plot_x0 + plot_w, y0 + row_h - 10], fill=(58, 58, 68))
            _text((plot_x0 + 6, y0 + row_h // 2 - 8), "(audio)", (130, 130, 140), f2)
            continue
        col = _PALETTE[i % len(_PALETTE)]
        for n in t.notes:
            x0 = plot_x0 + (float(n.start) / end) * plot_w
            x1 = plot_x0 + (float(n.end) / end) * plot_w
            if x1 < x0 + 2:
                x1 = x0 + 2
            frac = (int(n.pitch_midi) - lo) / max(1, hi - lo)
            y = y0 + (row_h - 8) - frac * (row_h - 16)
            d.rectangle([x0, y - 3, x1, y + 3], fill=col)

    # 小节线（按 tempo 粗画；4/4 计）
    try:
        tempo = float(score.tempo or 120.0)
        bar = 4 * 60.0 / tempo
        k = 1
        while True:
            x = plot_x0 + (k * bar / end) * plot_w
            if x >= plot_x0 + plot_w:
                break
            d.line([x, head_h, x, head_h + rows * row_h], fill=(48, 48, 54), width=1)
            k += 1
    except Exception:  # noqa: BLE001
        pass

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
