"""构建程序图标：多尺寸 PNG + 多尺寸 ICO。

只用项目 venv 里已有的 PySide6（QImage），不引入新依赖。
用法：
    .venv\\Scripts\\python.exe tools\\build_icon.py <源PNG> <输出目录>
"""
from __future__ import annotations

import os
import struct
import sys

from PySide6.QtGui import QImage
from PySide6.QtCore import Qt

SIZES = [16, 24, 32, 48, 64, 128, 256]
ALPHA_THRESHOLD = 12
PADDING_PERCENT = 4


def load_source(path: str) -> QImage:
    img = QImage(path)
    if img.isNull():
        raise SystemExit(f"无法读取图片：{path}")
    if img.format() != QImage.Format.Format_ARGB32:
        img = img.convertToFormat(QImage.Format.Format_ARGB32)
    return img


def alpha_bbox(img: QImage) -> tuple[int, int, int, int]:
    """返回 (x, y, w, h)：透明通道大于阈值的包围盒。"""
    width, height = img.width(), img.height()
    bits = img.constBits()
    data = bytes(bits)  # ARGB32 -> 内存序 BGRA
    stride = width * 4
    # 每像素 4 字节，alpha 位于第 4 个字节
    alpha = data[3::4]
    if len(alpha) != width * height:
        raise SystemExit("像素缓冲长度异常，无法定位图标边界")

    top = bottom = -1
    left, right = width, -1
    for y in range(height):
        row = alpha[y * width:(y + 1) * width]
        if max(row) <= ALPHA_THRESHOLD:
            continue
        if top < 0:
            top = y
        bottom = y
        first = next(i for i, v in enumerate(row) if v > ALPHA_THRESHOLD)
        last = next(i for i, v in enumerate(reversed(row)) if v > ALPHA_THRESHOLD)
        last = width - 1 - last
        if first < left:
            left = first
        if last > right:
            right = last

    if bottom < 0:
        raise SystemExit("图片全透明，放弃")
    del stride
    return left, top, right - left + 1, bottom - top + 1


def build_master(img: QImage) -> QImage:
    x, y, w, h = alpha_bbox(img)
    side = max(w, h)
    pad = int(round(side * PADDING_PERCENT / 100.0))
    out_side = side + pad * 2
    canvas = QImage(out_side, out_side, QImage.Format.Format_ARGB32)
    canvas.fill(0)  # 全透明
    target_x = (out_side - w) // 2
    target_y = (out_side - h) // 2
    # 用 QPainter 把裁好的主体画到正中
    from PySide6.QtGui import QPainter

    painter = QPainter(canvas)
    painter.drawImage(target_x, target_y, img, x, y, w, h)
    painter.end()
    return canvas


def write_png(master: QImage, size: int, path: str) -> None:
    scaled = master.scaled(
        size, size, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation
    )
    if not scaled.save(path, "PNG"):
        raise SystemExit(f"写 PNG 失败：{path}")


def write_ico(png_paths: list[tuple[int, str]], out_path: str) -> None:
    payloads = []
    for size, path in png_paths:
        with open(path, "rb") as fh:
            payloads.append((size, fh.read()))

    header = struct.pack("<HHH", 0, 1, len(payloads))
    entries = b""
    offset = 6 + 16 * len(payloads)
    for size, payload in payloads:
        dim = 0 if size >= 256 else size
        entries += struct.pack(
            "<BBBBHHII", dim, dim, 0, 0, 1, 32, len(payload), offset
        )
        offset += len(payload)

    with open(out_path, "wb") as fh:
        fh.write(header)
        fh.write(entries)
        for _, payload in payloads:
            fh.write(payload)


def main() -> int:
    if len(sys.argv) < 3:
        raise SystemExit("用法: build_icon.py <源PNG> <输出目录>")
    source, out_dir = sys.argv[1], sys.argv[2]

    icons_dir = os.path.join(out_dir, "icons")
    os.makedirs(icons_dir, exist_ok=True)

    master = build_master(load_source(source))
    print(f"母版尺寸: {master.width()}x{master.height()}")

    png_paths: list[tuple[int, str]] = []
    for size in SIZES:
        path = os.path.join(icons_dir, f"icon_{size}.png")
        write_png(master, size, path)
        png_paths.append((size, path))

    big = os.path.join(out_dir, "app_256.png")
    write_png(master, 256, big)

    ico_path = os.path.join(out_dir, "app.ico")
    write_ico(png_paths, ico_path)

    print(f"ICO: {ico_path}  {os.path.getsize(ico_path) / 1024:.1f} KB / {len(png_paths)} 个尺寸")
    for size, path in png_paths:
        print(f"  {size:>3}px  {os.path.getsize(path):>6} B  {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
