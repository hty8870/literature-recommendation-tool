from __future__ import annotations

import argparse
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

THEMES = (
    ("1", "简洁经典", "paper"),
    ("2", "农科院门户", "caas"),
    ("3", "学术札记", "editorial"),
    ("4", "研究工作台", "lab"),
)


def font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    name = "msyhbd.ttc" if bold else "msyh.ttc"
    return ImageFont.truetype(str(Path("C:/Windows/Fonts") / name), size)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("captures", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    receipt = json.loads((args.captures / "capture.json").read_text(encoding="utf-8"))
    assert len(receipt["messages"]) >= 3
    assert len(receipt["histories"]) >= 2

    width, height = 5000, 2600
    canvas = Image.new("RGB", (width, height), "#f3f4f1")
    draw = ImageDraw.Draw(canvas)
    draw.text((120, 62), "文献推荐工具 · 四种界面方案", font=font(54, bold=True), fill="#182c27")
    draw.text(
        (122, 146),
        "同一已登录账户  /  真实多轮对话  /  服务端持久化历史",
        font=font(28),
        fill="#52655e",
    )
    left, top = 150, 274
    column_width, row_height, gap = 1100, 928, 90
    for column, (choice, label, theme_id) in enumerate(THEMES):
        x = left + column * (column_width + gap)
        draw.rounded_rectangle((x, 230, x + 62, 292), radius=16, fill="#237c68")
        draw.text((x + 17, 241), choice, font=font(32, bold=True), fill="#ffffff")
        draw.text((x + 82, 239), label, font=font(38, bold=True), fill="#203d34")
        for row, (view, view_label) in enumerate((("home", "主页"), ("conversation", "对话页"))):
            y = top + row * (row_height + 150)
            draw.text((44, y + 18), view_label, font=font(30, bold=True), fill="#52655e")
            filename = args.captures / f"{choice}-{theme_id}-{view}.png"
            with Image.open(filename) as image:
                frame = image.convert("RGB").resize((column_width, 740), Image.Resampling.LANCZOS)
            canvas.paste(frame, (x, y + 68))
            draw.rounded_rectangle(
                (x, y + 68, x + column_width - 1, y + 807),
                radius=2,
                outline="#d7ddd8",
                width=2,
            )
    draw.line((120, 2430, 4880, 2430), fill="#d7ddd8", width=2)
    draw.text(
        (120, 2470),
        "真实 Europe PMC 题录快照 · 本地 Luna 模型生成 · "
        "摘要候选不等于全文证据 · 数值分数不在界面展示",
        font=font(25),
        fill="#66746d",
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(args.output, optimize=True)
    print(args.output)


if __name__ == "__main__":
    main()
