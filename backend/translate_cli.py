#!/usr/bin/env python3
"""
Simple CLI to run translations for a payload file without the UI/server.

Usage (mac):
  python translate_cli.py payload.json en hi ar ja pt

- payload.json should be an array of entries (same shape your app uses), e.g.:
  [ {"area_id":"title_a","text":"Texto de ejemplo","highlight_phrase":"ejemplo","color":"#fff"}, ... ]

This script:
- builds the notranslate set once from the payload,
- calls tradufotos.translate_variants for each entry and language,
- writes translations.json with results and prints a short summary to stdout.
"""
import sys
import json
import os
from pathlib import Path
from typing import List, Dict, Any

import tradufotos

def load_payload(path: str) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise SystemExit("payload.json must be a JSON array of entries.")
    return data

def main(argv):
    if len(argv) < 3:
        print("Usage: python translate_cli.py payload.json en hi ar ja pt")
        return
    payload_path = argv[1]
    langs = argv[2:]
    payload = load_payload(payload_path)
    print(f"[cli] Loaded payload with {len(payload)} entries.")
    # set notranslate once
    nt = tradufotos.set_notranslate_from_payload(payload)
    print(f"[cli] Built notranslate set ({len(nt)} tokens). Sample:", nt[:10])

    client = None
    try:
        client = tradufotos._get_openai_client()
        print("[cli] OpenAI client configured.")
    except Exception as e:
        print("[cli] OpenAI client not available; will attempt to use fallback. Error:", str(e))

    results = {}
    for lang in langs:
        print(f"[cli] Translating into {lang} ...")
        lang_results = []
        for entry in payload:
            text = entry.get("text") or ""
            variants = tradufotos.translate_variants(client, text, target_lang=lang, notranslate=nt)
            lang_results.append({
                "area_id": entry.get("area_id"),
                "original": text,
                "literal": variants.get("literal"),
                "human": variants.get("human"),
                "preserve": variants.get("preserve_tense_headline"),
            })
        results[lang] = lang_results
        print(f"[cli] Completed {len(lang_results)} entries for {lang}.")

    out_path = Path("translations_cli.json")
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"[cli] Wrote translations to {out_path.resolve()}")

if __name__ == "__main__":
    main(sys.argv)