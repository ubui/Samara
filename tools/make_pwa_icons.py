#!/usr/bin/env python3
"""生成 PWA 图标（纯标准库，无 Pillow 依赖）。

用法：python3 tools/make_pwa_icons.py
产出：web/static/img/pwa/icon-192.png、icon-512.png、
      icon-maskable-512.png、apple-touch-icon-180.png

设计：深色底 + 橄榄绿 "E"（elmsamaras 首字母），与 darkmatter 主题配色一致。
形状用有向距离场（SDF）光栅化，边缘自带 1px 抗锯齿。
"""

import math
import os
import struct
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "web", "static", "img", "pwa")

# 主题色（与 style.css 保持一致）
BG_TOP = (0x23, 0x22, 0x1B)
BG_BOTTOM = (0x12, 0x11, 0x0E)
FG_TOP = (0xD2, 0xEB, 0x79)
FG_BOTTOM = (0x7C, 0xA9, 0x10)


def sd_round_rect(px, py, cx, cy, hw, hh, r):
    """圆角矩形有向距离：内部为负，外部为正。"""
    dx = abs(px - cx) - (hw - r)
    dy = abs(py - cy) - (hh - r)
    ax = dx if dx > 0 else 0.0
    ay = dy if dy > 0 else 0.0
    return math.hypot(ax, ay) + min(max(dx, dy), 0.0) - r


def cover(d):
    """距离 -> 覆盖率（1px 过渡带）。"""
    c = 0.5 - d
    if c <= 0.0:
        return 0.0
    if c >= 1.0:
        return 1.0
    return c


def lerp(a, b, t):
    return a + (b - a) * t


# "E" 的四个笔画（基于 512 视口，等比缩放）
STROKES = (
    # cx,   cy,  hw,  hh,  r
    (181.0, 256.0, 25.0, 124.0, 16.0),   # 竖杠
    (276.0, 157.0, 80.0, 25.0, 16.0),    # 上横（左端伸入竖杠，避免接缝）
    (263.0, 256.0, 67.0, 25.0, 16.0),    # 中横
    (276.0, 355.0, 80.0, 25.0, 16.0),    # 下横
)


def render(size, rounded_ratio):
    """返回 RGBA 字节。rounded_ratio=0 表示满出血方形（maskable / iOS）。"""
    cx = cy = size / 2.0
    half = size / 2.0
    radius = size * rounded_ratio
    k = size / 512.0                       # 设计稿 -> 实际像素
    strokes = [(x * k, y * k, w * k, h * k, r * k) for x, y, w, h, r in STROKES]

    out = bytearray(size * size * 4)
    inv = 1.0 / (size - 1) if size > 1 else 0.0
    idx = 0
    for y in range(size):
        ty = y * inv
        for x in range(size):
            tx = x * inv
            # 背景：左上到右下的轻微渐变
            t = tx * 0.28 + ty * 0.72
            bg_r = lerp(BG_TOP[0], BG_BOTTOM[0], t)
            bg_g = lerp(BG_TOP[1], BG_BOTTOM[1], t)
            bg_b = lerp(BG_TOP[2], BG_BOTTOM[2], t)

            if radius > 0.0:
                bg_a = cover(sd_round_rect(x + 0.5, y + 0.5, cx, cy, half, half, radius))
            else:
                bg_a = 1.0

            # 前景：四个笔画取并集
            fg_a = 0.0
            for sx, sy, hw, hh, r in strokes:
                d = sd_round_rect(x + 0.5, y + 0.5, sx, sy, hw, hh, r)
                c = 0.5 - d
                if c > fg_a:                      # 快速跳过明显在外的像素
                    fg_a = 1.0 if c >= 1.0 else c
            if fg_a > 0.0:
                ft = min(max((y * inv - 0.24) / 0.52, 0.0), 1.0)
                fg_r = lerp(FG_TOP[0], FG_BOTTOM[0], ft)
                fg_g = lerp(FG_TOP[1], FG_BOTTOM[1], ft)
                fg_b = lerp(FG_TOP[2], FG_BOTTOM[2], ft)
                # 左上角柔光
                glow = max(0.0, 0.16 - 0.16 * (tx + ty) / 1.2)
                fg_r = min(255.0, fg_r + 255 * glow * 0.5)
                fg_g = min(255.0, fg_g + 255 * glow * 0.5)
                fg_b = min(255.0, fg_b + 255 * glow * 0.3)
                bg_r = lerp(bg_r, fg_r, fg_a)
                bg_g = lerp(bg_g, fg_g, fg_a)
                bg_b = lerp(bg_b, fg_b, fg_a)

            out[idx] = int(bg_r + 0.5)
            out[idx + 1] = int(bg_g + 0.5)
            out[idx + 2] = int(bg_b + 0.5)
            out[idx + 3] = int(255 * bg_a + 0.5)
            idx += 4
    return out


def write_png(path, size, rgba):
    stride = size * 4
    raw = bytearray()
    for y in range(size):
        raw.append(0)                            # filter: None
        raw += rgba[y * stride:(y + 1) * stride]
    comp = zlib.compress(bytes(raw), 9)

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", comp)
    png += chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(png)
    return len(png)


def main():
    os.makedirs(OUT, exist_ok=True)
    jobs = [
        ("icon-192.png", 192, 0.22),
        ("icon-512.png", 512, 0.22),
        ("icon-maskable-512.png", 512, 0.0),
        ("apple-touch-icon-180.png", 180, 0.0),
    ]
    for name, size, ratio in jobs:
        data = render(size, ratio)
        path = os.path.join(OUT, name)
        n = write_png(path, size, data)
        print("%-28s %4dx%-4d %7d bytes" % (name, size, size, n))


if __name__ == "__main__":
    main()
