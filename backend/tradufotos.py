#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
tradufotos.py — Spanish → 22 languages translation system

Uses:
- JSON prompts (flat A1 format, in naturalization_prompts.json)
- Forward translation (normal + strict)
- Refinement
- Triple backtranslation
- QA scoring
- Naturalization

Modes:
- headline          → translate()
- caption           → translate_caption()

Fully compatible with app.py:
- translate(text, lang)          for headlines / highlight_phrase / txt
- translate_caption(text, lang)  for Descripción de Foto (permanent_note)
"""

import os
import json
import sys
from difflib import SequenceMatcher
from typing import Optional, Dict, Any

from openai import OpenAI

# ============================================================
# CONFIG
# ============================================================

DEFAULT_MODEL = "gpt-4o"
SIMILARITY_THRESHOLD = 0.85

# JSON prompts stored in the SAME folder as this file
PROMPT_JSON_PATH = os.path.join(os.path.dirname(__file__), "naturalization_prompts.json")

# ============================================================
# LOGGING
# ============================================================

def _log_error(msg: str) -> None:
    try:
        sys.stderr.write(f"[tradufotos ERROR] {msg}\n")
        with open("tradufotos_errors.log", "a", encoding="utf-8") as f:
            f.write(f"[tradufotos ERROR] {msg}\n")
    except Exception:
        pass

# ============================================================
# LOAD PROMPTS
# ============================================================

def load_prompts() -> Dict[str, Any]:
    try:
        with open(PROMPT_JSON_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        raise RuntimeError(f"Could not load naturalization_prompts.json: {e}")

PROMPTS: Dict[str, Any] = load_prompts()

# Pre-cache supported languages from JSON
LANG_NAMES: Dict[str, str] = PROMPTS.get("languages", {})

# ============================================================
# OPENAI CLIENT
# ============================================================

def _get_client() -> OpenAI:
    """
    Create and return an OpenAI client using the OPENAI_API_KEY environment variable.

    Behavior:
    - First, check OPENAI_API_KEY in environment.
    - If not set, attempt to load a .env file via python-dotenv (if installed).
    - If still not set, raise a RuntimeError so callers fail fast.

    Note: Do NOT hardcode API keys. Use environment variables or secret managers.
    """
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        # Try to load a .env file if python-dotenv is available (optional convenience).
        try:
            from dotenv import load_dotenv  # type: ignore
            load_dotenv()
            api_key = os.environ.get("OPENAI_API_KEY")
        except Exception:
            pass

    if not api_key:
        raise RuntimeError("OPENAI_API_KEY environment variable not set. Please set it before calling translation functions.")

    return OpenAI(api_key=api_key)

# ============================================================
# HELPERS
# ============================================================

def _choice_content(resp) -> str:
    """Extract model response content safely."""
    try:
        return resp.choices[0].message.content.strip()
    except Exception:
        return str(resp)

def _similarity(a: str, b: str) -> float:
    """Compute normalized similarity between two texts."""
    return SequenceMatcher(
        None,
        " ".join(a.lower().split()),
        " ".join(b.lower().split())
    ).ratio()

def _extract_json(s: str) -> Optional[dict]:
    """Robust JSON extraction from possibly messy model output."""
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
                except Exception:
                    return None
    return None

# ============================================================
# PROMPT RETRIEVAL (MATCHES FLAT JSON)
# ============================================================

def get_prompt(prompt_key: str, lang: str) -> str:
    """
    Fetch a prompt from the JSON by top-level key.
    Example keys (must match JSON):
      - "headline_translation"
      - "headline_strict_translation"
      - "headline_refine"
      - "headline_backtranslation"
      - "headline_qa"
      - "headline_naturalization"
      - "caption_translation"
      - "caption_strict_translation"
      - "caption_refine"
      - "caption_backtranslation"
      - "caption_qa"
      - "caption_naturalization_mode_A"
    """
    if prompt_key not in PROMPTS:
        raise KeyError(f"Prompt key '{prompt_key}' not found in naturalization_prompts.json")

    node = PROMPTS[prompt_key]

    # node is expected to be a dict like:
    # { "system": "...", "special_rules": { "en": "..." } }
    if isinstance(node, dict):
        base = node.get("system", "")
        special = node.get("special_rules", {})
        extra = special.get(lang, "")
        if extra:
            base = base.rstrip() + "\n" + extra
    else:
        base = str(node)

    lang_name = LANG_NAMES.get(lang, lang)
    return base.replace("{{LANG_NAME}}", lang_name)

# ============================================================
# OPENAI CALL WRAPPER
# ============================================================

def _call(client: OpenAI, system_prompt: str, user_content: str, temp: float = 0.0):
    """Thin wrapper around Chat Completions API."""
    return client.chat.completions.create(
        model=DEFAULT_MODEL,
        temperature=temp,
        max_tokens=800,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content}
        ]
    )

# ============================================================
# PIPELINE COMPONENTS
# ============================================================

def forward_translate(client: OpenAI, text: str, lang: str, mode: str) -> str:
    """Normal translation (non-strict)."""
    if mode == "headline":
        sys_prompt = get_prompt("headline_translation", lang)
    else:
        sys_prompt = get_prompt("caption_translation", lang)

    resp = _call(client, sys_prompt, text)
    return _choice_content(resp)


def forward_translate_strict(client: OpenAI, text: str, lang: str, mode: str) -> str:
    """Strict translation."""
    if mode == "headline":
        sys_prompt = get_prompt("headline_strict_translation", lang)
    else:
        sys_prompt = get_prompt("caption_strict_translation", lang)

    resp = _call(client, sys_prompt, text)
    return _choice_content(resp)


def refine_translation(client: OpenAI, source: str, draft: str, lang: str, mode: str) -> str:
    """Refine output to improve naturalness without altering meaning."""
    if mode == "headline":
        sys_prompt = get_prompt("headline_refine", lang)
    else:
        sys_prompt = get_prompt("caption_refine", lang)

    try:
        resp = _call(
            client,
            sys_prompt,
            f"Spanish source:\n{source}\n\nDraft translation:\n{draft}"
        )
        out = _choice_content(resp)
        return out if out else draft
    except Exception:
        return draft


def back_translate(client: OpenAI, text: str, lang: str, mode: str) -> str:
    """Literal back-translation (mode-aware)."""
    if mode == "headline":
        sys_prompt = get_prompt("headline_backtranslation", lang)
    else:
        sys_prompt = get_prompt("caption_backtranslation", lang)

    resp = _call(client, sys_prompt, text)
    return _choice_content(resp)


def triple_backtranslation(client: OpenAI, text: str, lang: str, mode: str):
    """3 back-translations + stability score."""
    bt1 = back_translate(client, text, lang, mode)
    bt2 = back_translate(client, text, lang, mode)
    bt3 = back_translate(client, text, lang, mode)

    stability = (
        _similarity(bt1, bt2) +
        _similarity(bt1, bt3) +
        _similarity(bt2, bt3)
    ) / 3.0

    return bt1, stability


def qa_check(client: OpenAI, source: str, translated: str, sim: float, lang: str, mode: str) -> Dict[str, Any]:
    """Grammar, naturalness, and meaning preservation."""
    if mode == "headline":
        sys_prompt = get_prompt("headline_qa", lang)
    else:
        sys_prompt = get_prompt("caption_qa", lang)

    try:
        resp = _call(
            client,
            sys_prompt,
            f"Spanish: {source}\nTarget: {translated}\nSimilarity: {sim:.3f}"
        )
        parsed = _extract_json(_choice_content(resp))
        if not parsed:
            raise ValueError("Failed to parse QA JSON.")
        return {
            "naturalness": parsed.get("naturalness", 0),
            "grammar_ok": parsed.get("grammar_ok", False),
            "meaning_preserved": parsed.get("meaning_preserved", False),
            "raw": parsed
        }
    except Exception as e:
        _log_error(f"QA failed: {e}")
        return {
            "naturalness": 0,
            "grammar_ok": False,
            "meaning_preserved": False,
            "raw": {"error": "QA failed"}
        }

# ============================================================
# NATURALIZATION
# ============================================================

def naturalize(client: OpenAI, source: str, translation: str, lang: str, mode: str) -> str:
    """
    Final naturalization step:
    - Headlines → short, crisp, native journalistic tone
    - Captions → long-text naturalization (Mode A)
    """
    if mode == "headline":
        sys_prompt = get_prompt("headline_naturalization", lang)
    else:
        sys_prompt = get_prompt("caption_naturalization_mode_A", lang)

    try:
        resp = _call(client, sys_prompt, translation)
        out = _choice_content(resp)
        return out if out else translation
    except Exception:
        return translation

# ============================================================
# FULL PIPELINE
# ============================================================

def run_pipeline(text: str, lang: str, mode: str) -> Dict[str, Any]:
    """
    Full translation pipeline shared by:
      - translate()          → headlines
      - translate_caption()  → captions
    """
    if lang not in LANG_NAMES:
        raise ValueError(f"Unsupported target language code: {lang}")

    client = _get_client()

    # --- PASS 1: non-strict ---
    t1 = forward_translate(client, text, lang, mode)
    t1r = refine_translation(client, text, t1, lang, mode)

    bt1, stab1 = triple_backtranslation(client, t1r, lang, mode)
    sim1 = _similarity(text, bt1)
    qa1 = qa_check(client, text, t1r, sim1, lang, mode)

    verified1 = (
        sim1 >= SIMILARITY_THRESHOLD
        and qa1["grammar_ok"]
        and qa1["meaning_preserved"]
    )

    if verified1:
        final = naturalize(client, text, t1r, lang, mode)
        return {
            "human": final,
            "similarity": sim1,
            "stability": stab1,
            "back_translation": bt1,
            "qa_raw": qa1["raw"],
            "verified": True,
            "passes": 1,
            "target_lang": lang
        }

    # --- PASS 2: strict ---
    t2 = forward_translate_strict(client, text, lang, mode)
    t2r = refine_translation(client, text, t2, lang, mode)

    bt2, stab2 = triple_backtranslation(client, t2r, lang, mode)
    sim2 = _similarity(text, bt2)
    qa2 = qa_check(client, text, t2r, sim2, lang, mode)

    # choose best pass
    if sim2 > sim1:
        chosen = t2r
        chosen_bt = bt2
        chosen_sim = sim2
        chosen_stab = stab2
        chosen_qa = qa2
        verified = qa2["grammar_ok"] and qa2["meaning_preserved"]
    else:
        chosen = t1r
        chosen_bt = bt1
        chosen_sim = sim1
        chosen_stab = stab1
        chosen_qa = qa1
        verified = verified1

    final = naturalize(client, text, chosen, lang, mode)

    return {
        "human": final,
        "similarity": chosen_sim,
        "stability": chosen_stab,
        "back_translation": chosen_bt,
        "qa_raw": chosen_qa["raw"],
        "verified": verified,
        "passes": 2,
        "target_lang": lang
    }

# ============================================================
# PUBLIC API — HEADLINES
# ============================================================

def translate(text: str, lang: str) -> Dict[str, Any]:
    """
    Public API used by app.py for HEADLINES.
    """
    try:
        return run_pipeline(text, lang, mode="headline")
    except Exception as e:
        _log_error(f"translate() failed: {e}")
        return {"error": str(e)}

# ============================================================
# PUBLIC API — CAPTIONS
# ============================================================

def translate_caption(text: str, lang: str) -> Dict[str, Any]:
    """
    Public API used by app.py for CAPTIONS (Descripción de Foto).
    """
    try:
        return run_pipeline(text, lang, mode="caption")
    except Exception as e:
        _log_error(f"translate_caption() failed: {e}")
        return {"error": str(e)}

# ============================================================
# LEGACY COMPATIBILITY
# ============================================================

def translate_one(text: str, lang: str) -> Dict[str, Any]:
    """
    Legacy alias — behaves like translate() (headline mode).
    """
    return translate(text, lang)

# ============================================================
# CLI
# ============================================================

def main() -> None:
    """
    Usage:
        python tradufotos.py --lang=en "Un texto"
        python tradufotos.py --caption --lang=en "Una descripción larga"
    """
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python tradufotos.py --lang=en \"Texto\"")
        print("  python tradufotos.py --caption --lang=en \"Descripción\"")
        return

    lang = "en"
    mode = "headline"
    text_args = []

    for a in sys.argv[1:]:
        if a.startswith("--lang="):
            lang = a.split("=", 1)[1].strip()
        elif a == "--caption":
            mode = "caption"
        else:
            text_args.append(a)

    text = " ".join(text_args)

    if mode == "caption":
        result = translate_caption(text, lang)
    else:
        result = translate(text, lang)

    print(json.dumps(result, ensure_ascii=False, indent=2))

# ============================================================
# END OF MODULE
# ============================================================