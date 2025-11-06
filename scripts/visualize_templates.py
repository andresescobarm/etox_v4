#!/usr/bin/env python3
"""
visualize_templates.py

Generates overlay PNGs for each template (1080x1350).
Saves to outputs/{template_id}_1080x1350_overlay.png

Usage:
  python3 scripts/visualize_templates.py

Requirements:
  pip install pillow

This version uses a cross-version text-measure helper so it works with multiple Pillow versions.
"""
import json
import sys
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

CANONICAL_SIZE = (1080, 1350)
OUT_DIR = Path("outputs")
OUT_DIR.mkdir(exist_ok=True)

def compute_pixel_bbox(bbox, canvas_w, canvas_h):
    x = int(round(bbox["x_pct"] * canvas_w))
    y = int(round(bbox["y_pct"] * canvas_h))
    w = int(round(bbox["w_pct"] * canvas_w))
    h = int(round(bbox["h_pct"] * canvas_h))
    return (x,y,w,h)

def hex_to_rgba(h, alpha=255):
    h = h.lstrip("#")
    r = int(h[0:2],16)
    g = int(h[2:4],16)
    b = int(h[4:6],16)
    return (r,g,b,alpha)

def multiline_text_size(draw, text, font):
    """
    Cross-version compatible multiline text size measurement.
    Returns (width, height).
    """
    # Prefer the multiline bbox API if available
    if hasattr(draw, "multiline_textbbox"):
        try:
            l,t,r,b = draw.multiline_textbbox((0,0), text, font=font)
            return (r-l, b-t)
        except Exception:
            pass

    lines = text.splitlines() or [""]
    # Try to use font metrics for consistent line height
    try:
        ascent, descent = font.getmetrics()
        line_h_metric = ascent + descent
    except Exception:
        line_h_metric = None

    max_w = 0
    total_h = 0
    for i, line in enumerate(lines):
        w = h = 0
        # Try draw.textbbox (most recent)
        if hasattr(draw, "textbbox"):
            try:
                l,t,r,b = draw.textbbox((0,0), line, font=font)
                w = r - l
                h = b - t
            except Exception:
                pass
        # Fallback to draw.textsize
        if (w == 0 and h == 0) and hasattr(draw, "textsize"):
            try:
                w,h = draw.textsize(line, font=font)
            except Exception:
                pass
        # Fallback to font.getsize
        if (w == 0 and h == 0) and hasattr(font, "getsize"):
            try:
                w,h = font.getsize(line)
            except Exception:
                pass
        # Last resort: render to mask
        if (w == 0 and h == 0):
            try:
                mask = font.getmask(line)
                w,h = mask.size
            except Exception:
                w,h = 0,0

        if w > max_w:
            max_w = w
        # Use metric height if available, otherwise measured height
        if line_h_metric is not None:
            total_h += line_h_metric
        else:
            total_h += h

    return (max_w, total_h)

def main(path="templates/templates.json"):
    p = Path(path)
    if not p.exists():
        print("Templates file not found:", path)
        return 2
    data = json.loads(p.read_text(encoding="utf-8"))
    templates = data.get("templates", [])
    for t in templates:
        cw, ch = CANONICAL_SIZE
        img = Image.new("RGBA", (cw,ch), (240,240,240,255))
        draw = ImageDraw.Draw(img)
        # canvas border
        draw.rectangle([0,0,cw-1,ch-1], outline=(0,0,0,255), width=2)
        for a in t.get("text_areas", []):
            bbox = a.get("bbox", {})
            x,y,w,h = compute_pixel_bbox(bbox, cw, ch)
            # background fill if provided
            bg = a.get("background_color")
            if bg:
                try:
                    draw.rectangle([x,y,x+w,y+h], fill=hex_to_rgba(bg, alpha=255))
                except Exception:
                    pass
            # outline
            draw.rectangle([x,y,x+w,y+h], outline=(255,0,255,255), width=3)
            # label (id, name, pixel size)
            label = f"{a.get('id')}\n{a.get('name')}\n{w}x{h}"
            font = ImageFont.load_default()
            text_w, text_h = multiline_text_size(draw, label, font)
            tx = x + 6
            ty = y + 6
            # label background
            draw.rectangle([tx-2, ty-2, tx+text_w+2, ty+text_h+2], fill=(255,255,255,230))
            draw.multiline_text((tx,ty), label, fill=(0,0,0), font=font)
        out_path = OUT_DIR / f"{t['id']}_1080x1350_overlay.png"
        img.convert("RGB").save(out_path, format="PNG")
        print("Saved:", out_path)
    return 0

if __name__ == "__main__":
    sys.exit(main())