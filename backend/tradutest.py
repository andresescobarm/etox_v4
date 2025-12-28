#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
tradufotos.py — Standard Tier:    Spanish → 22 languages
2 passes:    Translate → Naturalize (parallel processing)
+ Double validation for EN, DE, ZH-TW (Option C)
Target: 98-99% quality | Max cost: ~$0.09/headline + validation

Modes:
  --csv "headline"           → Translate single headline, output CSV
  --batch input.csv          → Translate all headlines from CSV, output CSV
  --lang=XX "headline"       → Translate single headline to one language (JSON)
"""

import os
import json
import sys
import csv
import asyncio
from typing import Dict, Any, List

from openai import AsyncOpenAI

# ============================================================
# CONFIG
# ============================================================

DEFAULT_MODEL = "gpt-5.1"
API_KEY = "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"
PROMPT_JSON_PATH = os.path.join(os.path.dirname(__file__), "naturalization_prompts.json")

# Languages requiring double validation
VALIDATION_LANGUAGES = {"en", "de", "zh-TW"}

# ============================================================
# VALIDATION PROMPTS
# ============================================================

VALIDATOR_1_PROMPT = """You are a translation quality validator. 

Compare the Initial translation to the Final (naturalized) translation.

Check for these ERRORS in the Final version:
1.  TENSE CHANGE: Past became present, present became past, future changed
2. MEANING CHANGE: Facts, names, numbers, or qualifiers added/removed
3. SUBJECT CHANGE: Key subject word changed (mother→woman, smile→girl, husband→other)
4. VOICE CHANGE: Active became passive or vice versa
5. ADDITION:  Words added that weren't in Initial (adjectives, context, outcomes, "felt", "died")
6. SYNONYM SWAP: Correct word unnecessarily changed (crazy→insane, height→stature, farms→farming)
7. ARTICLE CHANGE: "the" became "a" or articles removed from proper names
8. RELATIONSHIP CHANGE: Family relationships altered (son→father, daughter→mother)

For German: Check if "wird" became "wurde" (tense error)
For Chinese: Check if "感到" was added, or "不治" (died) was added after injury
For English: Check for unnecessary synonym swaps or restructuring

Respond with ONLY one word:  
- PASS (if Final is correct or improved)
- FAIL (if Final has any error listed above)"""

VALIDATOR_2_PROMPT = """You are a senior translation reviewer.

Your job:  Verify the naturalization did NOT break the translation.

Compare Initial vs Final and check:  
1. Did the tense stay the same? (past=past, present=present, future=future)
2. Did the core meaning stay identical? (no facts added or removed)
3. Did key subjects stay the same? (mother, father, husband, smile, woman, man)
4. Did the voice stay the same? (active/passive unchanged)
5. Were words added that shouldn't be?  (emotional words, outcomes, context)
6. Were correct words swapped for synonyms unnecessarily? 

Language-specific checks:  
- German:  "wird" must NOT become "wurde"
- Chinese: "感到" must NOT be added; "不治" must NOT be added after injuries
- English:  Minimal changes only; no synonym swaps for correct words

Respond with ONLY one word:  
- PASS (naturalization is valid)
- FAIL (naturalization broke something)"""

# ============================================================
# LOAD PROMPTS
# ============================================================

def load_prompts() -> Dict[str, Any]:
    try:
        with open(PROMPT_JSON_PATH, "r", encoding="utf-8") as f:
            return json. load(f)
    except Exception as e:
        raise RuntimeError(f"Could not load prompts:  {e}")

PROMPTS = load_prompts()
LANG_NAMES = PROMPTS. get("languages", {})

# ============================================================
# ASYNC CLIENT
# ============================================================

def _get_client() -> AsyncOpenAI:
    return AsyncOpenAI(api_key=API_KEY)

# ============================================================
# CORE ASYNC CALL
# ============================================================

async def _call_llm(client:  AsyncOpenAI, system_prompt: str, 
                    user_content: str, temperature: float = 0.2) -> str:
    try:
        response = await client.chat.completions.create(
            model=DEFAULT_MODEL,
            temperature=temperature,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content}
            ]
        )
        return response.choices[0].message.content. strip()
    except Exception as e: 
        return f"Error: {str(e)}"

# ============================================================
# VALIDATION
# ============================================================

async def _validate(client: AsyncOpenAI, initial:  str, final: str, lang: str) -> bool:
    """
    Run double validation on Initial vs Final. 
    Returns True if both validators pass, False if either fails.
    """
    # Pre-check: if no changes made, auto-pass
    if initial. strip() == final.strip():
        return True
    
    lang_name = LANG_NAMES. get(lang, lang)
    
    user_content = f"""Language: {lang_name}

Initial translation:
{initial}

Final (naturalized) translation:
{final}"""

    # Run both validators in parallel
    v1_task = _call_llm(client, VALIDATOR_1_PROMPT, user_content, temperature=0.0)
    v2_task = _call_llm(client, VALIDATOR_2_PROMPT, user_content, temperature=0.0)
    
    v1_result, v2_result = await asyncio.gather(v1_task, v2_task)
    
    v1_pass = "PASS" in v1_result. upper()
    v2_pass = "PASS" in v2_result.upper()
    
    return v1_pass and v2_pass

# ============================================================
# PROMPT BUILDER WITH SHARED RULES
# ============================================================

def _build_shared_rules(lang: str) -> str:
    """Build shared rules that apply to both translation and naturalization."""
    shared = PROMPTS.get("shared_rules", {})
    lang_name = LANG_NAMES.get(lang, lang)
    
    rules_parts = []
    
    # Core rules (always included)
    if "core" in shared:
        rules_parts.append(shared["core"])
    
    # Language-specific shared rules
    if "language_specific" in shared and lang in shared["language_specific"]: 
        rules_parts. append(shared["language_specific"][lang])
    
    combined = "\n\n".join(rules_parts)
    combined = combined.replace("{{LANG_NAME}}", lang_name)
    
    return combined

def _get_prompt(prompt_key: str, lang:  str) -> str:
    """
    Build the system prompt for a given prompt key and language.
    Injects shared rules and language-specific rules. 
    """
    prompt_data = PROMPTS. get(prompt_key, {})
    system_prompt = prompt_data.get("system", "")
    lang_name = LANG_NAMES.get(lang, lang)
    
    # Get shared rules
    shared_rules = _build_shared_rules(lang)
    
    # Get prompt-specific language rules if they exist
    lang_specific_rules = ""
    if "language_specific_rules" in prompt_data:
        lang_specific_rules = prompt_data["language_specific_rules"]. get(lang, "")
    
    # Replace placeholders
    system_prompt = system_prompt.replace("{{LANG_NAME}}", lang_name)
    system_prompt = system_prompt.replace("{{SHARED_RULES}}", shared_rules)
    system_prompt = system_prompt.replace("{{LANG_SPECIFIC_RULES}}", lang_specific_rules)
    
    # Clean up empty lines from missing optional sections
    lines = system_prompt. split("\n")
    cleaned_lines = [line for line in lines if line.strip() or line == ""]
    system_prompt = "\n".join(cleaned_lines)
    
    return system_prompt

# ============================================================
# STANDARD TIER:    2-PASS PIPELINE + VALIDATION
# ============================================================

async def translate_standard(client: AsyncOpenAI, text: str, lang: str) -> Dict[str, str]:
    """
    Standard Tier: 2 passes + validation for EN, DE, ZH-TW
    1. Initial translation
    2. Naturalization
    3. Double validation (EN, DE, ZH-TW only) → revert if fail
    """
    lang_name = LANG_NAMES.get(lang, lang)
    
    # Pass 1: Translate
    prompt_translate = _get_prompt("headline_translation", lang)
    initial = await _call_llm(client, prompt_translate, text, temperature=0.2)
    
    # Pass 2: Naturalize
    prompt_naturalize = _get_prompt("headline_naturalization", lang)
    final = await _call_llm(client, prompt_naturalize, initial, temperature=0.15)
    
    # Pass 3: Validate (only for EN, DE, ZH-TW)
    validated = True
    if lang in VALIDATION_LANGUAGES:
        validated = await _validate(client, initial, final, lang)
        if not validated: 
            final = initial  # Revert to Initial if validation fails
    
    return {
        "lang": lang,
        "lang_name": lang_name,
        "initial": initial,
        "final": final,
        "validated": validated
    }

# ============================================================
# PARALLEL PROCESSING
# ============================================================

async def translate_all_parallel(text: str) -> List[Dict[str, str]]:
    """Translate one headline to all 22 languages in parallel."""
    client = _get_client()
    tasks = [translate_standard(client, text, lang) for lang in LANG_NAMES]
    results = await asyncio.gather(*tasks)
    return results

async def translate_headline_with_original(text: str) -> List[Dict[str, str]]:
    """Translate one headline and include original text in results."""
    results = await translate_all_parallel(text)
    for r in results:
        r["original"] = text
    return results

async def translate_batch_parallel(headlines: List[str]) -> List[Dict[str, str]]: 
    """
    Translate multiple headlines, each to all 22 languages. 
    Processes headlines sequentially, but each headline's 22 languages in parallel.
    """
    all_results = []
    total = len(headlines)
    
    for i, headline in enumerate(headlines, 1):
        print(f"[{i}/{total}] Translating: \"{headline[: 50]}...\"" if len(headline) > 50 else f"[{i}/{total}] Translating: \"{headline}\"")
        results = await translate_headline_with_original(headline)
        all_results.extend(results)
        
        # Count validation reverts
        reverts = sum(1 for r in results if r.get("validated") == False)
        if reverts: 
            print(f"         ✓ Completed 22 translations ({reverts} reverted to Initial)")
        else:
            print(f"         ✓ Completed 22 translations")
    
    return all_results

# ============================================================
# CSV IMPORT
# ============================================================

def load_headlines_from_csv(input_file: str) -> List[str]: 
    """
    Load headlines from CSV file.
    Expects a column named 'headline' (case-insensitive).
    """
    headlines = []
    try:
        with open(input_file, mode="r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            
            # Find the headline column (case-insensitive)
            fieldnames_lower = {name.lower(): name for name in reader.fieldnames}
            headline_col = fieldnames_lower.get("headline")
            
            if not headline_col: 
                raise ValueError(f"CSV must have a 'headline' column.  Found: {reader.fieldnames}")
            
            for row in reader:
                headline = row[headline_col].strip()
                if headline:
                    headlines.append(headline)
        
        print(f"✅ Loaded {len(headlines)} headlines from '{input_file}'")
        return headlines
    
    except FileNotFoundError:
        raise FileNotFoundError(f"❌ File not found: {input_file}")
    except Exception as e: 
        raise RuntimeError(f"❌ Error reading CSV: {e}")

# ============================================================
# CSV EXPORT
# ============================================================

def generate_csv(text: str, output_file: str = "translations.csv") -> None:
    """Generate CSV for a single headline."""
    print(f"Translating: \"{text}\"")
    print(f"Languages: {len(LANG_NAMES)}")
    print(f"Validation:  EN, DE, ZH-TW (double-check)")
    print("Processing with parallel execution...")
    
    results = asyncio.run(translate_all_parallel(text))
    
    try:
        with open(output_file, mode="w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["Language Code", "Language Name", "Initial", "Final Translation", "Validated"])
            for r in results:
                writer.writerow([r["lang"], r["lang_name"], r["initial"], r["final"], r. get("validated", "N/A")])
        
        reverts = sum(1 for r in results if r.get("validated") == False)
        print(f"\n✅ CSV '{output_file}' created with {len(results)} translations.")
        if reverts:
            print(f"   ⚠️  {reverts} translation(s) reverted to Initial due to validation failure.")
    except Exception as e: 
        print(f"❌ Failed to write CSV: {e}")

def generate_batch_csv(headlines:  List[str], output_file: str = "batch_translations.csv") -> None:
    """Generate CSV for multiple headlines."""
    print(f"\n{'='*60}")
    print(f"BATCH TRANSLATION")
    print(f"{'='*60}")
    print(f"Headlines:  {len(headlines)}")
    print(f"Languages: {len(LANG_NAMES)}")
    print(f"Validation:  EN, DE, ZH-TW (double-check)")
    print(f"Total translations: {len(headlines) * len(LANG_NAMES)}")
    print(f"{'='*60}\n")
    
    results = asyncio.run(translate_batch_parallel(headlines))
    
    try:
        with open(output_file, mode="w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["Original", "Language Code", "Language Name", "Initial", "Final", "Validated"])
            for r in results:
                writer. writerow([r["original"], r["lang"], r["lang_name"], r["initial"], r["final"], r.get("validated", "N/A")])
        
        reverts = sum(1 for r in results if r. get("validated") == False)
        
        print(f"\n{'='*60}")
        print(f"✅ COMPLETE")
        print(f"{'='*60}")
        print(f"Output: {output_file}")
        print(f"Total rows: {len(results)}")
        if reverts:
            print(f"⚠️  Reverted to Initial:  {reverts}")
        print(f"{'='*60}")
    except Exception as e:
        print(f"❌ Failed to write CSV: {e}")

# ============================================================
# CLI
# ============================================================

def main() -> None:
    if len(sys.argv) < 2:
        print("Usage:")
        print('  python tradufotos.py --csv "Spanish headline"')
        print('  python tradufotos.py --batch input.csv')
        print('  python tradufotos.py --batch input.csv --output results.csv')
        print('  python tradufotos.py --lang=tr "Spanish headline"')
        return

    args = sys.argv[1:]
    
    # Batch mode:  --batch input.csv [--output output.csv]
    if "--batch" in args: 
        batch_index = args.index("--batch")
        
        # Get input file
        if batch_index + 1 >= len(args):
            print("❌ Error: --batch requires an input CSV file")
            print('   Example: python tradufotos.py --batch headlines.csv')
            return
        
        input_file = args[batch_index + 1]
        
        # Get output file (optional)
        output_file = "batch_translations.csv"
        if "--output" in args: 
            output_index = args.index("--output")
            if output_index + 1 < len(args):
                output_file = args[output_index + 1]
        
        # Load and process
        try:
            headlines = load_headlines_from_csv(input_file)
            if not headlines:
                print("❌ No headlines found in CSV")
                return
            generate_batch_csv(headlines, output_file)
        except Exception as e:
            print(str(e))
            return
    
    # Single headline CSV mode
    elif "--csv" in args: 
        args.remove("--csv")
        text = " ".join(args)
        generate_csv(text)
    
    # Single language mode
    else:
        lang = "en"
        for arg in args[: ]:
            if arg.startswith("--lang="):
                lang = arg.split("=", 1)[1].strip()
                args.remove(arg)
        text = " ".join(args)
        result = asyncio.run(translate_standard(_get_client(), text, lang))
        print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == "__main__": 
    main()