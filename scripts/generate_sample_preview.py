#!/usr/bin/env python3
"""
generate_sample_preview.py

Usage:
  pip install requests pillow
  python scripts/generate_sample_preview.py --template template_A_portrait_1080x1350

It will:
- create sample_input.png sized to the template
- POST to http://localhost:8000/render-preview with a basic payload for each area
- Save returned preview to sample_preview.png
"""
import argparse
import json
from pathlib import Path
from PIL import Image, ImageDraw
import requests

API_URL = "http://localhost:8000/render-preview"

def create_sample_input(path, w, h):
    im = Image.new("RGB", (w,h), (245,245,255))
    draw = ImageDraw.Draw(im)
    # draw subtle guide lines
    for i in range(0, w, 120):
        draw.line([(i,0),(i,h)], fill=(235,235,255))
    for j in range(0, h, 120):
        draw.line([(0,j),(w,j)], fill=(235,235,255))
    im.save(path, format="PNG")

def build_payload_for_template(template):
    payload = []
    for a in template.get("text_areas", []):
        # sample text (keeps short to test sizing)
        sample_text = a.get("name", a["id"])
        payload.append({
            "area_id": a["id"],
            "text": sample_text,
            "lang": "default",
            "color": template.get("allowed_colors", ["#000000"])[0],
            "font_size": int((a.get("min_font_size",20)+a.get("max_font_size",40))/2),
            "dx_pct": 0.0,
            "dy_pct": 0.0
        })
    return payload

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--template", required=True)
    args = parser.parse_args()
    tfile = Path("templates/templates.json")
    if not tfile.exists():
        print("templates/templates.json missing.")
        return
    data = json.loads(tfile.read_text(encoding="utf-8"))
    templates = {t["id"]: t for t in data.get("templates", [])}
    if args.template not in templates:
        print("Template id not found. Available:", ", ".join(templates.keys()))
        return
    template = templates[args.template]
    w = template["size"]["width"]
    h = template["size"]["height"]
    sample_path = Path("sample_input.png")
    create_sample_input(sample_path, w, h)
    payload = build_payload_for_template(template)
    files = {"image": open(sample_path, "rb")}
    data = {"template_id": template["id"], "payload": json.dumps(payload)}
    print("Posting to", API_URL)
    res = requests.post(API_URL, data=data, files=files)
    if not res.ok:
        print("Render failed:", res.status_code, res.text)
        return
    out = Path("sample_preview.png")
    out.write_bytes(res.content)
    print("Saved preview to", out)

if __name__ == "__main__":
    main()