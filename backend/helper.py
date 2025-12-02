#!/usr/bin/env python3
import requests
import json
import sys
import os

url = "http://127.0.0.1:8000/debug-request"
img_path = "/Users/upsomedia/Downloads/avril.png"   # change if needed

payload = [
    {
        "area_id": "main_box",
        "text": "Avril Lavigne presentó su primer vino...",
        "color": "#FFFFFF",
        "font_size": 56
    }
]

data = {
    "template_id": "template_A_portrait_1080x1350",
    "payload": json.dumps(payload)
}

try:
    with open(img_path, "rb") as f:
        files = {"image": f}
        r = requests.post(url, data=data, files=files, timeout=30)
    print("Status:", r.status_code)
    print(r.text)
except Exception as e:
    print("Error:", e)
    sys.exit(1)