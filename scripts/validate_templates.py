#!/usr/bin/env python3
"""
validate_templates.py

Usage:
  python scripts/validate_templates.py templates/templates.json

Validates:
- bbox fields exist and are within [0,1]
- boxes fit inside the 1080x1350 canvas
- prints exact pixel bbox values for each text area
"""
import json
import sys
from pathlib import Path

CANVAS = (1080, 1350)

def compute_pixel_bbox(bbox, canvas_w, canvas_h):
    x = int(round(bbox["x_pct"] * canvas_w))
    y = int(round(bbox["y_pct"] * canvas_h))
    w = int(round(bbox["w_pct"] * canvas_w))
    h = int(round(bbox["h_pct"] * canvas_h))
    return (x,y,w,h)

def validate_template_file(path):
    p = Path(path)
    if not p.exists():
        print("File not found:", path)
        return 2
    data = json.loads(p.read_text(encoding="utf-8"))
    templates = data.get("templates", [])
    errors = []
    print("Validating templates for canvas:", CANVAS)
    for t in templates:
        print("\nTemplate:", t.get("id"), "-", t.get("name"))
        for a in t.get("text_areas", []):
            bbox = a.get("bbox", {})
            if any(k not in bbox for k in ("x_pct","y_pct","w_pct","h_pct")):
                errors.append(f"{t['id']}/{a.get('id')}: missing bbox keys")
                continue
            x_pct = bbox["x_pct"]; y_pct = bbox["y_pct"]; w_pct = bbox["w_pct"]; h_pct = bbox["h_pct"]
            if not (0 <= x_pct <= 1):
                errors.append(f"{t['id']}/{a.get('id')}: x_pct out of range: {x_pct}")
            if not (0 <= y_pct <= 1):
                errors.append(f"{t['id']}/{a.get('id')}: y_pct out of range: {y_pct}")
            if not (0 < w_pct <= 1):
                errors.append(f"{t['id']}/{a.get('id')}: w_pct out of range: {w_pct}")
            if not (0 < h_pct <= 1):
                errors.append(f"{t['id']}/{a.get('id')}: h_pct out of range: {h_pct}")
            if x_pct + w_pct > 1.00001:
                errors.append(f"{t['id']}/{a.get('id')}: x_pct + w_pct > 1.0")
            if y_pct + h_pct > 1.00001:
                errors.append(f"{t['id']}/{a.get('id')}: y_pct + h_pct > 1.0")
            x,y,w,h = compute_pixel_bbox(bbox, CANVAS[0], CANVAS[1])
            print(f"  area {a.get('id')}: x={x}, y={y}, w={w}, h={h}  (x+w={x+w}, y+h={y+h})")
    if errors:
        print("\nValidation errors found:")
        for e in errors:
            print(" -", e)
        return 1
    print("\nAll templates validated successfully for canvas", CANVAS)
    return 0

if __name__ == "__main__":
    sys.exit(validate_template_file("templates/templates.json"))