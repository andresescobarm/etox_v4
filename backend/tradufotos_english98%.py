#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
tradufotos.py — Spanish → 22-language high-fidelity translation engine
with strict translation, refinement, triple back-translation, QA,
and final viral-news naturalization (Option 4, refined).
Includes CSV batch mode for testing headlines.
"""

import os
import sys
import json
import csv
import traceback
from difflib import SequenceMatcher
from typing import Dict, Any, Optional

from openai import OpenAI

DEFAULT_MODEL = "gpt-4o"
SIMILARITY_THRESHOLD = 0.85

LANG_TABLE = {
    "ar": ("Arabic", "Arabic"),
    "bn": ("Bengali", "Bengali"),
    "nl": ("Dutch", "Dutch"),
    "en": ("English", "English"),
    "fr": ("French", "French"),
    "de": ("German", "German"),
    "he": ("Hebrew", "Hebrew"),
    "hi": ("Hindi", "Hindi"),
    "id": ("Indonesian", "Indonesian"),
    "it": ("Italian", "Italian"),
    "ja": ("Japanese", "Japanese"),
    "ms": ("Malay", "Malay"),
    "pl": ("Polish", "Polish"),
    "pt": ("Portuguese", "Portuguese"),
    "ro": ("Romanian", "Romanian"),
    "sv": ("Swedish", "Swedish"),
    "tl": ("Tagalog", "Tagalog"),
    "zh-TW": ("Chinese (Traditional)", "Chinese (Traditional)"),
    "th": ("Thai", "Thai"),
    "tr": ("Turkish", "Turkish"),
    "uk": ("Ukrainian", "Ukrainian"),
    "vi": ("Vietnamese", "Vietnamese"),
}

LANG_FILENAME_MAP = {
    "ar": "arabic",
    "bn": "bengali",
    "nl": "dutch",
    "en": "english",
    "fr": "french",
    "de": "german",
    "he": "hebrew",
    "hi": "hindi",
    "id": "indonesian",
    "it": "italian",
    "ja": "japanese",
    "ms": "malay",
    "pl": "polish",
    "pt": "portuguese",
    "ro": "romanian",
    "sv": "swedish",
    "tl": "tagalog",
    "zh-TW": "chinese_traditional",
    "th": "thai",
    "tr": "turkish",
    "uk": "ukrainian",
    "vi": "vietnamese",
}

def _log_error(msg: str) -> None:
    try:
        sys.stderr.write(f"[tradufotos ERROR] {msg}\n")
        with open("tradufotos_errors.log", "a", encoding="utf-8") as f:
            f.write(f"[tradufotos ERROR] {msg}\n")
    except:
        pass

def _get_client():
    API_KEY = "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"  # ← Replace
    return OpenAI(api_key=API_KEY)

def _choice_content(resp):
    try:
        return resp.choices[0].message.content.strip()
    except:
        return str(resp)

def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None,
        " ".join(a.lower().split()),
        " ".join(b.lower().split())
    ).ratio()

def _extract_json(s: str) -> Optional[dict]:
    try:
        start = s.index("{")
    except ValueError:
        return None

    depth = 0
    in_str = False
    esc = False

    for i in range(start, len(s)):
        ch = s[i]
        if ch == '"' and not esc:
            in_str = not in_str
        if ch == "\\" and not esc:
            esc = True
            continue
        esc = False
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(s[start:i+1])
                except:
                    return None
    return None

def build_translation_prompt(lang, lname):
    return (
        f"You are a professional Spanish → {lname} translator.\n"
        "Translate naturally and idiomatically.\n"
        "Preserve all meaning, facts, entities, numbers, and tense.\n"
        "Return ONLY the translation."
    )

def build_strict_translation_prompt(lang, lname):
    return (
        f"You are a strict Spanish → {lname} translator.\n"
        "Translate as literally as possible while remaining grammatically correct.\n"
        "Return ONLY the translation."
    )

def build_refine_prompt(lname):
    return (
        f"Refine the {lname} translation so it reads natural, fluent, and idiomatic.\n"
        "Do NOT add or remove meaning.\n"
        "Return ONLY the refined sentence."
    )

def build_back_prompt(lname):
    return f"Back-translate {lname} → Spanish literally. Return ONLY Spanish."

def build_qa_prompt(lname):
    return (
        "Return ONLY JSON:\n"
        "{ naturalness: 1-10, grammar_ok: true/false, meaning_preserved: true/false }"
    )

# 🔥 UPDATED NATURALIZATION PROMPT (STRICTER, SAFER, MORE ACCURATE)
def build_naturalization_prompt(lname):
    return (
        f"You are a senior news & viral-content editor writing headlines in {lname}.\n"
        "Rewrite the translation into a fully natural, idiomatic headline.\n"
        "STYLE: Balanced viral + news tone (Insider, People.com, Distractify).\n"
        "ABSOLUTE RULES:\n"
        "- Do NOT add emotion, outrage, shock, humor, or implications not in the Spanish.\n"
        "- Do NOT add or remove any information.\n"
        "- Do NOT exaggerate, sensationalize, soften, or editorialize.\n"
        "- Preserve any uncertainty (e.g., 'habría' → 'may have').\n"
        "- Preserve who does what (no flipping subjects).\n"
        "- Preserve all facts, entities, numbers, relationships, and tense.\n"
        "- No metaphors, no creative interpretations.\n"
        "- Just produce a clean, natural, fluent headline.\n"
        "Return ONLY the final improved headline."
    )

def _call(client, messages, temp=0):
    return client.chat.completions.create(
        model=DEFAULT_MODEL,
        messages=messages,
        temperature=temp,
        max_tokens=512,
    )

def forward_translate(client, text, lang, lname, strict=False):
    sys_prompt = (
        build_strict_translation_prompt(lang, lname)
        if strict else build_translation_prompt(lang, lname)
    )
    resp = _call(client, [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": text}
    ])
    return _choice_content(resp)

def refine(client, src, draft, lname):
    try:
        resp = _call(client, [
            {"role": "system", "content": build_refine_prompt(lname)},
            {"role": "user", "content": f"Spanish: {src}\nDraft: {draft}"}
        ])
        out = _choice_content(resp)
        return out if out else draft
    except:
        return draft

def back_translate(client, text, lname):
    resp = _call(client, [
        {"role": "system", "content": build_back_prompt(lname)},
        {"role": "user", "content": text}
    ])
    return _choice_content(resp)

def triple_backtranslation(client, text, lname):
    bt1 = back_translate(client, text, lname)
    bt2 = back_translate(client, text, lname)
    bt3 = back_translate(client, text, lname)
    stab = (
        _similarity(bt1, bt2)
        + _similarity(bt1, bt3)
        + _similarity(bt2, bt3)
    ) / 3
    return bt1, stab

def qa_check(client, source, translated, sim, lname):
    try:
        resp = _call(client, [
            {"role": "system", "content": build_qa_prompt(lname)},
            {"role": "user", "content": f"Spanish: {source}\nTarget: {translated}\nSimilarity: {sim:.3f}"}
        ])
        parsed = _extract_json(_choice_content(resp))
        if not parsed:
            raise Exception("QA parse error")
        return {
            "naturalness": parsed.get("naturalness", 0),
            "grammar_ok": parsed.get("grammar_ok", False),
            "meaning_preserved": parsed.get("meaning_preserved", False)
        }
    except:
        return {"naturalness": 0, "grammar_ok": False, "meaning_preserved": False}

def final_naturalize(client, source, translation, lname):
    try:
        resp = _call(client, [
            {"role": "system", "content": build_naturalization_prompt(lname)},
            {"role": "user", "content": translation}
        ])
        improved = _choice_content(resp)
        return improved if improved else translation
    except:
        return translation

def translate_one(text, lang):
    client = _get_client()
    lname, internal = LANG_TABLE[lang]

    t1 = forward_translate(client, text, lang, internal, strict=False)
    t1r = refine(client, text, t1, internal)
    bt1, stab1 = triple_backtranslation(client, t1r, internal)
    sim1 = _similarity(text, bt1)
    qa1 = qa_check(client, text, t1r, sim1, internal)
    verified1 = (sim1 >= SIMILARITY_THRESHOLD and qa1["grammar_ok"] and qa1["meaning_preserved"])
    chosen = t1r if verified1 else None

    if not verified1:
        t2 = forward_translate(client, text, lang, internal, strict=True)
        t2r = refine(client, text, t2, internal)
        bt2, stab2 = triple_backtranslation(client, t2r, internal)
        sim2 = _similarity(text, bt2)
        qa2 = qa_check(client, text, t2r, sim2, internal)
        chosen = t2r if sim2 >= sim1 else t1r

    naturalized = final_naturalize(client, text, chosen, internal)

    return {"human": naturalized, "target_lang": lang}

def translate_csv(input_file, lang):
    if not os.path.isfile(input_file):
        raise FileNotFoundError(f"Input CSV not found: {input_file}")

    downloads = os.path.join(os.path.expanduser("~"), "Downloads")
    if not os.path.isdir(downloads):
        raise FileNotFoundError("Downloads folder does not exist.")

    if lang not in LANG_FILENAME_MAP:
        raise ValueError(f"Unsupported lang code: {lang}")

    output_name = f"output_testing_{LANG_FILENAME_MAP[lang]}.csv"
    output_path = os.path.join(downloads, output_name)

    rows = []
    with open(input_file, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if "spanish" not in reader.fieldnames:
            raise ValueError("CSV must contain a 'spanish' column.")
        for row in reader:
            rows.append(row["spanish"])

    total = len(rows)
    print(f"Processing {total} headlines...\n")

    results = []

    for i, text in enumerate(rows, start=1):
        print(f"[{i}/{total}] Translating: {text}")
        translated = translate_one(text, lang)
        print("→ Done\n")
        results.append([text, translated["human"]])

    with open(output_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["spanish", LANG_FILENAME_MAP[lang]])
        writer.writerows(results)

    print(f"\n✅ Translation complete.\nSaved to: {output_path}")

def main():
    if len(sys.argv) < 2:
        print("Usage:")
        print("python tradufotos.py --input=yourfile.csv --lang=en")
        return

    input_file = None
    lang = "en"

    for a in sys.argv[1:]:
        if a.startswith("--input="):
            input_file = a.split("=", 1)[1]
        elif a.startswith("--lang="):
            lang = a.split("=", 1)[1]

    if input_file:
        translate_csv(input_file, lang)
        return

    text = " ".join([a for a in sys.argv[1:] if not a.startswith("--")])
    print(json.dumps(translate_one(text, lang), ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
