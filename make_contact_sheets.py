#!/usr/bin/env python3
"""Tile photos downloaded by --save-images into numbered contact sheets for quick review."""

import argparse
import json
import os

from PIL import Image, ImageDraw


def build_contact_sheets(images_dir, out_dir, cols=5, rows=4, thumb_size=(220, 165)):
    with open(os.path.join(images_dir, "manifest.json")) as f:
        manifest = json.load(f)

    os.makedirs(out_dir, exist_ok=True)
    per_sheet = cols * rows
    sheet_paths = []

    for start in range(0, len(manifest), per_sheet):
        chunk = manifest[start:start + per_sheet]
        sheet = Image.new("RGB", (cols * thumb_size[0], rows * thumb_size[1]), "white")
        draw = ImageDraw.Draw(sheet)

        for i, entry in enumerate(chunk):
            img_path = os.path.join(images_dir, entry["file"])
            try:
                img = Image.open(img_path).convert("RGB")
            except (OSError, ValueError):
                continue
            img.thumbnail((thumb_size[0] - 4, thumb_size[1] - 18))
            x = (i % cols) * thumb_size[0]
            y = (i // cols) * thumb_size[1]
            sheet.paste(img, (x + 2, y + 2))
            draw.rectangle([x, y + thumb_size[1] - 16, x + 28, y + thumb_size[1]], fill="black")
            draw.text((x + 3, y + thumb_size[1] - 15), str(start + i), fill="yellow")

        sheet_path = os.path.join(out_dir, f"sheet_{start:04d}.jpg")
        sheet.save(sheet_path, quality=85)
        sheet_paths.append(sheet_path)

    return sheet_paths


def main():
    parser = argparse.ArgumentParser(
        description="Tile photos from a --save-images directory into numbered contact "
                    "sheets, so many photos can be reviewed at a glance instead of one by one."
    )
    parser.add_argument("images_dir", help="Directory produced by --save-images "
                         "(must contain manifest.json)")
    parser.add_argument("--out-dir", default=None,
                         help="Where to write sheets (default: <images_dir>/contact_sheets)")
    parser.add_argument("--cols", type=int, default=5)
    parser.add_argument("--rows", type=int, default=4)
    args = parser.parse_args()

    out_dir = args.out_dir or os.path.join(args.images_dir, "contact_sheets")
    sheets = build_contact_sheets(args.images_dir, out_dir, cols=args.cols, rows=args.rows)
    print(f"Wrote {len(sheets)} contact sheet(s) to {out_dir}")
    print("Each thumbnail is labeled with its index into manifest.json for cross-reference.")


if __name__ == "__main__":
    main()
