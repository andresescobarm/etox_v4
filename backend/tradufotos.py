#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import sys
from typing import Dict, Any

from openai import OpenAI

# Use a valid model for chat completions
DEFAULT_MODEL = "gpt-4o"

# Keeping the key hardcoded as requested (for local testing only).
API_KEY = os.environ.get(
    "OPENAI_API_KEY",
    "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",
)

PROMPT_JSON_PATH = os.path.join(os.path.dirname(__file__), "naturalization_prompts.json")

VALIDATION_LANGUAGES = {"en", "de", "zh-TW"}


def load_prompts() -> Dict[str, Any]:
    try:
        with open(PROMPT_JSON_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        raise RuntimeError(f"Could not load prompts: {e}")


PROMPTS = load_prompts()
LANG_NAMES = PROMPTS.get("languages", {})


def _get_client() -> OpenAI:
    return OpenAI(api_key=API_KEY)


def _call_llm(client: OpenAI, system_prompt: str, user_content: str, temperature: float = 0.2) -> str:
    try:
        response = client.chat.completions.create(
            model=DEFAULT_MODEL,
            temperature=temperature,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        # Return a clear error marker; caller will sanitize
        return f"Error: {str(e)}"


def _build_shared_rules(lang: str) -> str:
    shared = PROMPTS.get("shared_rules", {})
    lang_name = LANG_NAMES.get(lang, lang)
    rules_parts = []
    if "core" in shared:
        rules_parts.append(shared["core"])
    if "language_specific" in shared and lang in shared["language_specific"]:
        rules_parts.append(shared["language_specific"][lang])
    combined = "\n\n".join(rules_parts)
    combined = combined.replace("{{LANG_NAME}}", lang_name)
    return combined


def _get_prompt(prompt_key: str, lang: str) -> str:
    prompt_data = PROMPTS.get(prompt_key, {})
    system_prompt = prompt_data.get("system", "")
    lang_name = LANG_NAMES.get(lang, lang)
    shared_rules = _build_shared_rules(lang)
    lang_specific_rules = ""
    if "language_specific_rules" in prompt_data:
        lang_specific_rules = prompt_data["language_specific_rules"].get(lang, "")
    system_prompt = system_prompt.replace("{{LANG_NAME}}", lang_name)
    system_prompt = system_prompt.replace("{{SHARED_RULES}}", shared_rules)
    system_prompt = system_prompt.replace("{{LANG_SPECIFIC_RULES}}", lang_specific_rules)
    lines = system_prompt.split("\n")
    cleaned_lines = [line for line in lines if line.strip() or line == ""]
    return "\n".join(cleaned_lines)


def translate_standard(client: OpenAI, text: str, lang: str) -> Dict[str, str]:
    lang_name = LANG_NAMES.get(lang, lang)
    prompt_translate = _get_prompt("headline_translation", lang)
    initial = _call_llm(client, prompt_translate, text, temperature=0.2)
    prompt_naturalize = _get_prompt("headline_naturalization", lang)
    final = _call_llm(client, prompt_naturalize, initial, temperature=0.15)
    return {
        "lang": lang,
        "lang_name": lang_name,
        "initial": initial,
        "final": final,
        "validated": True,
    }


def _clean_translation_result(result, original_text):
    """Prevent propagating LLM error strings as translations."""
    if isinstance(result, dict):
        human = result.get("final") or result.get("human")
        if human and not str(human).startswith("Error:"):
            return human
        return original_text
    if isinstance(result, str):
        if result.startswith("Error:"):
            return original_text
        return result
    return original_text


def translate(text: str, lang: str) -> dict:
    if not text or not text.strip():
        return {"human": text, "initial": text, "validated": True}
    if lang == "es":
        return {"human": text, "initial": text, "validated": True}
    try:
        client = _get_client()
        result = translate_standard(client, text, lang)
        cleaned = _clean_translation_result(result, text)
        return {
            "human": cleaned,
            "initial": result.get("initial", text),
            "validated": result.get("validated", True),
        }
    except Exception as e:
        print(f"Translation error: {e}")
        return {"human": text, "initial": text, "validated": False, "error": str(e)}


def translate_caption(text: str, lang: str) -> dict:
    return translate(text, lang)


if __name__ == "__main__":
    if len(sys.argv) >= 2:
        text = " ".join(sys.argv[1:])
        result = translate(text, "en")
        print(json.dumps(result, ensure_ascii=False, indent=2))