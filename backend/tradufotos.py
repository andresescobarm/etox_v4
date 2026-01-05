#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PRODUCTION-READY TRANSLATION SYSTEM
====================================

Key improvements over original:
1. Environment variable for API key (security)
2. Rate limiting (10 calls/sec to avoid OpenAI limits)
3. Retry logic with exponential backoff
4. Structured error logging to JSONL
5. Cost tracking and monitoring
6. Sequential language processing (cost optimization)
7. Detailed timing and diagnostics

Author: Production Team
Date: 2026-01-04
"""

import asyncio
import os
import json
from typing import Dict, Any, List, Optional
from datetime import datetime
from time import time

from openai import OpenAI, AsyncOpenAI

from rate_limiter import RateLimiter, RetryHandler
from error_logger import ErrorLogger

from .cache_manager import get_cache_manager

# Initialize cache on module load
_ = get_cache_manager()

# ============================================================
# CONFIG
# ============================================================

DEFAULT_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.1")
API_KEY = os.getenv("OPENAI_API_KEY")

if not API_KEY:
    raise ValueError(
        "OPENAI_API_KEY environment variable is required. "
        "Set it before running:  export OPENAI_API_KEY='your-key-here'"
    )

PROMPT_JSON_PATH = os.path.join(os.path.dirname(__file__), "naturalization_prompts.json")

# Languages requiring double validation
VALIDATION_LANGUAGES = {"en", "de", "fr", "zh-TW"}

# ============================================================
# GLOBAL INSTANCES
# ============================================================

# Rate limiter:  10 calls per second (conservative for GPT-5. 1)
rate_limiter = RateLimiter(calls_per_second=10.0)

# Error logger: logs to errors.jsonl
error_logger = ErrorLogger("errors.jsonl")

# Retry handler: 3 attempts with exponential backoff
retry_handler = RetryHandler(max_retries=3, backoff_base=2.0)

# ============================================================
# COST TRACKING
# ============================================================

# GPT-5.1 pricing per 1M tokens
COST_INPUT_PER_1M = 1.25   # $1.25 per 1M input tokens
COST_OUTPUT_PER_1M = 10.00  # $10.00 per 1M output tokens

def estimate_cost(input_tokens: int, output_tokens: int) -> float:
    """
    Estimate cost in USD for a given token count.
    
    Args:
        input_tokens: Number of input tokens
        output_tokens:  Number of output tokens
        
    Returns:
        Estimated cost in USD
    """
    input_cost = (input_tokens / 1_000_000) * COST_INPUT_PER_1M
    output_cost = (output_tokens / 1_000_000) * COST_OUTPUT_PER_1M
    return input_cost + output_cost

# ============================================================
# LOAD PROMPTS
# ============================================================

def load_prompts() -> Dict[str, Any]:
    try:
        with open(PROMPT_JSON_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        raise RuntimeError(f"Could not load prompts:  {e}")

PROMPTS = load_prompts()
LANG_NAMES = PROMPTS. get("languages", {})

# ============================================================
# CLIENT
# ============================================================

def _get_client() -> OpenAI:
    """Get configured OpenAI client instance."""
    return OpenAI(api_key=API_KEY, timeout=30.0)  # 30 second timeout

def _get_async_client() -> AsyncOpenAI:
    """Get configured async OpenAI client instance."""
    return AsyncOpenAI(api_key=API_KEY, timeout=30.0)  # 30 second timeout


# ============================================================
# CORE LLM CALL WITH RATE LIMITING & RETRY
# ============================================================



def _call_llm(
    client:  OpenAI,
    system_prompt: str,
    user_content: str,
    temperature:  float = 0.2
) -> Dict[str, Any]: 
    """
    Call LLM with rate limiting, retry logic, and token tracking.
    
    Returns:
        Dict with keys: 'content', 'input_tokens', 'output_tokens', 'total_tokens'
    """
    
    def api_call():
        # Wait if we're calling too fast
        rate_limiter.wait_if_needed()
        
        # Make the API call
        response = client.chat.completions.create(
            model=DEFAULT_MODEL,
            temperature=temperature,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
        )
        
        # Extract content and token usage
        content = response.choices[0]. message.content. strip()
        
        # Token usage (may be None in some cases)
        usage = response.usage
        input_tokens = usage. prompt_tokens if usage else 0
        output_tokens = usage. completion_tokens if usage else 0
        total_tokens = usage. total_tokens if usage else 0
        
        return {
            "content": content,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens
        }
    
    try:
        return retry_handler.execute(api_call)
    except Exception as e:
        # Return error marker if all retries failed
        return {
            "content": f"Error: {str(e)}",
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0
        }


async def _call_llm_async(
    client: AsyncOpenAI,
    system_prompt: str,
    user_content: str,
    temperature:  float = 0.3
) -> dict:
    """
    Async version:  Call OpenAI LLM with retry logic and token tracking.
    Returns dict with: content, input_tokens, output_tokens, total_tokens
    """
    retry_handler = RetryHandler(max_retries=3, backoff_base=2.0)
    
    async def api_call():
        # Add async rate limiting
        await asyncio.sleep(rate_limiter.min_interval)
        
        response = await client.chat.completions.create(
            model=DEFAULT_MODEL,
            temperature=temperature,
            messages=[
    
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
        )
        
        # Extract content and token usage
        content = response.choices[0]. message.content.strip()
        
        # Token usage (may be None in some cases)
        usage = response.usage
        input_tokens = usage.prompt_tokens if usage else 0
        output_tokens = usage.completion_tokens if usage else 0
        total_tokens = usage.total_tokens if usage else 0
        
        return {
            "content": content,
            "input_tokens":  input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens
        }
    
    try:
        return await retry_handler.execute_async(api_call)
    except Exception as e:
        # Return error marker if all retries failed
        return {
            "content": f"Error: {str(e)}",
            "input_tokens": 0,
            "output_tokens":  0,
            "total_tokens": 0
        }


# ============================================================
# VALIDATION
# ============================================================

VALIDATOR_1_PROMPT = """You are a translation quality validator. 

Compare the Initial translation to the Final (naturalized) translation.

Check for these ERRORS in the Final version:
1. TENSE CHANGE:  Past became present, present became past, future changed
2. MEANING CHANGE: Facts, names, numbers, or qualifiers added/removed
3. SUBJECT CHANGE: Key subject word changed (mother→woman, smile→girl, husband→other)
4. VOICE CHANGE: Active became passive or vice versa
5. ADDITION: Words added that weren't in Initial (adjectives, context, outcomes, "felt", "died")
6. SYNONYM SWAP:  Correct word unnecessarily changed (crazy→insane, height→stature, farms→farming)
7. ARTICLE CHANGE: "the" became "a" or articles removed from proper names
8. RELATIONSHIP CHANGE: Family relationships altered (son→father, daughter→mother)

For German:  Check if "wird" became "wurde" (tense error)
For Chinese: Check if "感到" was added, or "不治" (died) was added after injury
For English: Check for unnecessary synonym swaps or restructuring

Respond with ONLY one word: 
- PASS (if Final is correct or improved)
- FAIL (if Final has any error listed above)"""

VALIDATOR_2_PROMPT = """You are a senior translation reviewer.

Your job:  Verify the naturalization did NOT break the translation.

Compare Initial vs Final and check: 
1. Did the tense stay the same?  (past=past, present=present, future=future)
2. Did the core meaning stay identical? (no facts added or removed)
3. Did key subjects stay the same? (mother, father, husband, smile, woman, man)
4. Did the voice stay the same? (active/passive unchanged)
5. Were words added that shouldn't be?  (emotional words, outcomes, context)
6. Were correct words swapped for synonyms unnecessarily? 

Language-specific checks:
- German: "wird" must NOT become "wurde"
- Chinese:  "感到" must NOT be added; "不治" must NOT be added after injuries
- English:  Minimal changes only; no synonym swaps for correct words

Respond with ONLY one word:
- PASS (naturalization is valid)
- FAIL (naturalization broke something)"""

def _validate(
    client: OpenAI,
    initial:  str,
    final: str,
    lang:  str,
    token_tracker: Dict[str, int]
) -> bool:
    """
    Validate that naturalization didn't break the translation.
    
    Args:
        client: OpenAI client
        initial: Initial translation
        final: Final (naturalized) translation
        lang:  Language code
        token_tracker:  Dict to accumulate token counts
        
    Returns: 
        True if validation passed, False otherwise
    """
    if initial. strip() == final.strip():
        return True

    lang_name = LANG_NAMES.get(lang, lang)
    user_content = f"""Language: {lang_name}

Initial translation: 
{initial}

Final (naturalized) translation:
{final}"""

    # Run both validators
    v1_response = _call_llm(client, VALIDATOR_1_PROMPT, user_content, temperature=0.0)
    v2_response = _call_llm(client, VALIDATOR_2_PROMPT, user_content, temperature=0.0)
    
    # Track tokens
    token_tracker["input_tokens"] += v1_response["input_tokens"] + v2_response["input_tokens"]
    token_tracker["output_tokens"] += v1_response["output_tokens"] + v2_response["output_tokens"]
    token_tracker["total_tokens"] += v1_response["total_tokens"] + v2_response["total_tokens"]
    
    v1_result = v1_response["content"]
    v2_result = v2_response["content"]
    
    v1_pass = "PASS" in v1_result. upper()
    v2_pass = "PASS" in v2_result.upper()
    
    # Log validation failure
    if not (v1_pass and v2_pass):
        error_logger. log_validation_failure(
            lang, initial, final, v1_result, v2_result
        )
    
    return v1_pass and v2_pass


async def _validate_async(
    client: AsyncOpenAI,
    initial:  str,
    final: str,
    lang: str,
    token_tracker: Dict[str, int]
) -> bool:
    """
    Async version:  Validate that naturalization didn't break the translation. 
    
    Returns: 
        True if validation passed, False otherwise
    """
    if initial.strip() == final.strip():
        return True

    lang_name = LANG_NAMES. get(lang, lang)
    user_content = f"""Language:  {lang_name}

Initial translation: 
{initial}

Final (naturalized) translation:
{final}"""

    # Run both validators concurrently
    v1_task = _call_llm_async(client, VALIDATOR_1_PROMPT, user_content, temperature=0.0)
    v2_task = _call_llm_async(client, VALIDATOR_2_PROMPT, user_content, temperature=0.0)
    
    # Await both
    v1_response, v2_response = await asyncio.gather(v1_task, v2_task)
    
    # Track tokens
    token_tracker["input_tokens"] += v1_response["input_tokens"] + v2_response["input_tokens"]
    token_tracker["output_tokens"] += v1_response["output_tokens"] + v2_response["output_tokens"]
    token_tracker["total_tokens"] += v1_response["total_tokens"] + v2_response["total_tokens"]
    
    v1_result = v1_response["content"]
    v2_result = v2_response["content"]
    
    v1_pass = "PASS" in v1_result. upper()
    v2_pass = "PASS" in v2_result.upper()
    
    # Log validation failure
    if not (v1_pass and v2_pass):
        error_logger. log_validation_failure(
            lang, initial, final, v1_result, v2_result
        )
    
    return v1_pass and v2_pass


# ============================================================
# PROMPT BUILDERS
# ============================================================

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

# ============================================================
# STANDARD TIER:  2-PASS + VALIDATION
# ============================================================


def translate_standard(
    client: OpenAI,
    text: str,
    lang: str,
    user_id: Optional[str] = None
) -> Dict[str, Any]:


    """
    Standard Tier:  2 passes + validation for EN, DE, ZH-TW
    
    1. Initial translation
    2. Naturalization
    3. Double validation (EN, DE, ZH-TW only) → revert if fail
    
    Args:
        client: OpenAI client
        text: Text to translate
        lang: Target language code
        user_id: Optional user identifier for logging
        
    Returns:
        Dict with translation results and diagnostics
    """
    start_time = time()
    lang_name = LANG_NAMES.get(lang, lang)
    
    # Token tracking
    token_tracker = {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0
    }


    try:
        # Pass 1: Initial translation
        prompt_translate = _get_prompt("headline_translation", lang)
        response1 = _call_llm(client, prompt_translate, text, temperature=0.1)
        initial = response1["content"]
        
        # Track tokens
        token_tracker["input_tokens"] += response1["input_tokens"]
        token_tracker["output_tokens"] += response1["output_tokens"]
        token_tracker["total_tokens"] += response1["total_tokens"]
        
        if initial.startswith("Error:"):
            raise Exception(initial)

        # Pass 2: Naturalization
        prompt_naturalize = _get_prompt("headline_naturalization", lang)
        response2 = _call_llm(client, prompt_naturalize, initial, temperature=0.1)
        final = response2["content"]
        
        # Track tokens
        token_tracker["input_tokens"] += response2["input_tokens"]
        token_tracker["output_tokens"] += response2["output_tokens"]
        token_tracker["total_tokens"] += response2["total_tokens"]
        
        if final.startswith("Error:"):
            raise Exception(final)

        # Pass 3: Validation (only for specific languages)
        validated = True
        if lang in VALIDATION_LANGUAGES:
            validated = _validate(client, initial, final, lang, token_tracker)
            if not validated:
                final = initial  # revert if validation fails

        elapsed = time() - start_time
        
        # Estimate cost
        estimated_cost = estimate_cost(
            token_tracker["input_tokens"],
            token_tracker["output_tokens"]
        )
        
        # Log cost for monitoring
        error_logger.log_cost_estimate(
            text, token_tracker["total_tokens"], estimated_cost, user_id
        )
        
        return {
            "lang": lang,
            "lang_name": lang_name,
            "initial": initial,
            "final": final,
            "validated": validated,
            "elapsed_seconds": round(elapsed, 2),
            "input_tokens": token_tracker["input_tokens"],
            "output_tokens": token_tracker["output_tokens"],
            "total_tokens": token_tracker["total_tokens"],
            "estimated_cost_usd": round(estimated_cost, 4),
            "error": None
        }
        
    except Exception as e:
        elapsed = time() - start_time
        error_logger.log_translation_error(text, lang, str(e), user_id)
        
        return {
            "lang":  lang,
            "lang_name": lang_name,
            "initial": text,
            "final": text,
            "validated": False,
            "elapsed_seconds": round(elapsed, 2),
            "input_tokens": token_tracker["input_tokens"],
            "output_tokens": token_tracker["output_tokens"],
            "total_tokens": token_tracker["total_tokens"],
            "estimated_cost_usd": 0.0,
            "error": str(e)
        }



def translate_description_internal(
    client: OpenAI,
    text: str,
    lang: str,
    user_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Description translation:  uses description_translation and description_naturalization prompts. 
    Separate from headline translation logic.
    """
    start_time = time()
    lang_name = LANG_NAMES.get(lang, lang)
    
    token_tracker = {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0
    }

    try:
        # Pass 1: Initial translation (using description_translation prompt)
        prompt_translate = _get_prompt("description_translation", lang)
        response1 = _call_llm(client, prompt_translate, text, temperature=0.2)
        initial = response1["content"]
        
        token_tracker["input_tokens"] += response1["input_tokens"]
        token_tracker["output_tokens"] += response1["output_tokens"]
        token_tracker["total_tokens"] += response1["total_tokens"]
        
        if initial.startswith("Error:"):
            raise Exception(initial)

        # Pass 2: Naturalization (using description_naturalization prompt)
        prompt_naturalize = _get_prompt("description_naturalization", lang)
        response2 = _call_llm(client, prompt_naturalize, initial, temperature=0.15)
        final = response2["content"]
        
        token_tracker["input_tokens"] += response2["input_tokens"]
        token_tracker["output_tokens"] += response2["output_tokens"]
        token_tracker["total_tokens"] += response2["total_tokens"]
        
        if final.startswith("Error:"):
            raise Exception(final)

        # Pass 3: Validation (same as headlines - only for specific languages)
        validated = True
        if lang in VALIDATION_LANGUAGES:
            validated = _validate(client, initial, final, lang, token_tracker)
            if not validated:
                final = initial

        elapsed = time() - start_time
        estimated_cost = estimate_cost(
            token_tracker["input_tokens"],
            token_tracker["output_tokens"]
        )
        
        error_logger.log_cost_estimate(
            text, token_tracker["total_tokens"], estimated_cost, user_id
        )
        
        return {
            "lang": lang,
            "lang_name": lang_name,
            "initial": initial,
            "final": final,
            "validated": validated,
            "elapsed_seconds": round(elapsed, 2),
            "input_tokens": token_tracker["input_tokens"],
            "output_tokens": token_tracker["output_tokens"],
            "total_tokens": token_tracker["total_tokens"],
            "estimated_cost_usd": round(estimated_cost, 4),
            "error": None
        }
        
    except Exception as e: 
        elapsed = time() - start_time
        error_logger.log_translation_error(text, lang, str(e), user_id)
        
        return {
            "lang": lang,
            "lang_name": lang_name,
            "initial": text,
            "final": text,
            "validated": False,
            "elapsed_seconds": round(elapsed, 2),
            "input_tokens": token_tracker["input_tokens"],
            "output_tokens": token_tracker["output_tokens"],
            "total_tokens": token_tracker["total_tokens"],
            "estimated_cost_usd": 0.0,
            "error": str(e)
        }

async def translate_standard_async(
    client: AsyncOpenAI,
    text: str,
    lang: str,
    user_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Async version of translate_standard for concurrent processing. 
    
    Returns: 
        Dict with translation results and diagnostics
    """
    start_time = time()
    lang_name = LANG_NAMES. get(lang, lang)
    
    # Token tracking
    token_tracker = {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0
    }

    try: 
        # Pass 1: Initial translation
        prompt_translate = _get_prompt("headline_translation", lang)
        response1 = await _call_llm_async(client, prompt_translate, text, temperature=0.2)
        initial = response1["content"]
        
        # Track tokens
        token_tracker["input_tokens"] += response1["input_tokens"]
        token_tracker["output_tokens"] += response1["output_tokens"]
        token_tracker["total_tokens"] += response1["total_tokens"]
        
        if initial.startswith("Error:"):
            raise Exception(initial)

        # Pass 2: Naturalization
        prompt_naturalize = _get_prompt("headline_naturalization", lang)
        response2 = await _call_llm_async(client, prompt_naturalize, initial, temperature=0.15)
        final = response2["content"]
        
        # Track tokens
        token_tracker["input_tokens"] += response2["input_tokens"]
        token_tracker["output_tokens"] += response2["output_tokens"]
        token_tracker["total_tokens"] += response2["total_tokens"]
        
        if final.startswith("Error:"):
            raise Exception(final)

        # Pass 3: Validation (only for specific languages)
        validated = True
        if lang in VALIDATION_LANGUAGES:
            validated = await _validate_async(client, initial, final, lang, token_tracker)
            if not validated:
                final = initial  # revert if validation fails

        elapsed = time() - start_time
        
        # Estimate cost
        estimated_cost = estimate_cost(
            token_tracker["input_tokens"],
            token_tracker["output_tokens"]
        )
        
        # Log cost for monitoring
        error_logger.log_cost_estimate(
            text, token_tracker["total_tokens"], estimated_cost, user_id
        )
        
        return {
            "lang": lang,
            "lang_name": lang_name,
            "initial": initial,
            "final": final,
            "validated": validated,
            "elapsed_seconds": round(elapsed, 2),
            "input_tokens": token_tracker["input_tokens"],
            "output_tokens":  token_tracker["output_tokens"],
            "total_tokens": token_tracker["total_tokens"],
            "estimated_cost_usd": round(estimated_cost, 4),
            "error": None
        }
        
    except Exception as e:
        elapsed = time() - start_time
        error_logger.log_translation_error(text, lang, str(e), user_id)
        
        return {
            "lang":  lang,
            "lang_name": lang_name,
            "initial": text,
            "final": text,
            "validated":  False,
            "elapsed_seconds": round(elapsed, 2),
            "input_tokens": token_tracker["input_tokens"],
            "output_tokens": token_tracker["output_tokens"],
            "total_tokens": token_tracker["total_tokens"],
            "estimated_cost_usd": 0.0,
            "error": str(e)
        }


# ============================================================
# SEQUENTIAL PROCESSING (COST-OPTIMIZED)
# ============================================================

def translate_all_sequential(
    text: str,
    user_id: Optional[str] = None
) -> List[Dict[str, Any]]: 
    """
    Process all languages SEQUENTIALLY (one at a time).
    This is the cost-optimized approach that stays under rate limits.
    
    Args:
        text: Text to translate
        user_id: Optional user identifier
        
    Returns:
        List of translation results for all languages
    """
    client = _get_client()
    results = []
    langs = list(LANG_NAMES.keys())
    
    total_start = time()
    
    print(f"\n[TRANSLATE] Starting sequential translation for {len(langs)} languages")
    print(f"[TRANSLATE] User: {user_id or 'anonymous'}")
    print(f"[TRANSLATE] Text: {text[: 100]}..." if len(text) > 100 else f"[TRANSLATE] Text: {text}")
    
    # Process each language one at a time
    for idx, lang in enumerate(langs, 1):
        lang_start = time()
        
        result = translate_standard(client, text, lang, user_id)
        results.append(result)
        
        lang_elapsed = time() - lang_start
        
        # Progress indicator
        status = "✓" if not result. get("error") else "✗"
        print(f"[{idx}/{len(langs)}] {status} {lang: 6s} | "
              f"{lang_elapsed: 5.2f}s | "
              f"{result. get('total_tokens', 0):5d} tokens | "
              f"${result.get('estimated_cost_usd', 0):.4f}")
    
    total_elapsed = time() - total_start
    total_tokens = sum(r.get("total_tokens", 0) for r in results)
    total_cost = sum(r.get("estimated_cost_usd", 0) for r in results)
    
    print(f"\n[TRANSLATE] Completed in {total_elapsed:.2f}s")
    print(f"[TRANSLATE] Total tokens: {total_tokens}")
    print(f"[TRANSLATE] Estimated cost: ${total_cost:. 4f}")
    
    return results


async def translate_all_concurrent(
    text: str,
    user_id: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    Process all languages CONCURRENTLY using async.
    This is faster but uses more API quota.
    
    Args:
        text: Text to translate
        user_id: Optional user identifier
        
    Returns:
        List of translation results for all languages
    """
    client = _get_async_client()
    langs = list(LANG_NAMES.keys())
    
    total_start = time()
    
    print(f"\n[TRANSLATE] Starting CONCURRENT translation for {len(langs)} languages")
    print(f"[TRANSLATE] User: {user_id or 'anonymous'}")
    print(f"[TRANSLATE] Text: {text[: 100]}..." if len(text) > 100 else f"[TRANSLATE] Text: {text}")
    
    # Create tasks for all languages
    tasks = [
        translate_standard_async(client, text, lang, user_id)
        for lang in langs
    ]
    
    # Run all concurrently
    results = await asyncio.gather(*tasks)
    
    total_elapsed = time() - total_start
    total_tokens = sum(r.get("total_tokens", 0) for r in results)
    total_cost = sum(r.get("estimated_cost_usd", 0) for r in results)
    
    print(f"\n[TRANSLATE] ✅ CONCURRENT completed in {total_elapsed:.2f}s")
    print(f"[TRANSLATE] Total tokens:  {total_tokens}")
    print(f"[TRANSLATE] Estimated cost: ${total_cost:.4f}")
    speedup = (len(langs) * 2.0) / total_elapsed if total_elapsed > 0 else 0
    print(f"[TRANSLATE] Speedup: {speedup:.1f}x faster than sequential")
    return list(results)


# ============================================================
# WRAPPERS FOR APP. PY (BACKWARD COMPATIBILITY)
# ============================================================

def _is_mostly_caps(s: str, threshold: float = 0.7) -> bool:
    letters = [ch for ch in s if ch.isalpha()]
    if not letters:
        return False
    caps = sum(1 for ch in letters if ch.isupper())
    return caps / len(letters) >= threshold


def translate(text: str, lang: str, user_id: Optional[str] = None) -> dict:
    """
    Synchronous wrapper used by app.py with caching.
    Returns dict:   {"human": <final>, "initial": <initial>, "validated": <bool>}
    
    This maintains backward compatibility with existing app.py code.
    """
    if not text or not text. strip():
        return {"human":  text, "initial": text, "validated": True}
    if lang == "es":  
        return {"human": text, "initial": text, "validated": True}
    
    # ✅ CHECK CACHE FIRST
    try:
        from .cache_manager import get_cache_manager
        cache = get_cache_manager()
        cached = cache.get_translation(text, lang, "headline")
        if cached:
            return cached
    except Exception as e: 
        print(f"⚠️  Cache check failed: {e}")
    
    try:
        client = _get_client()
        result = translate_standard(client, text, lang, user_id)
        final = result. get("final", text)
        initial = result.get("initial", text)
        validated = result.get("validated", True)

        # Preserve all-caps style if source was mostly caps (skip scripts without casing)
        if _is_mostly_caps(text) and lang not in ("zh", "zh-TW", "zh-CN", "ja", "ko", "ar", "he", "fa", "ur"):
            final = final.upper()
            initial = initial.upper()

        if isinstance(final, str) and final.startswith("Error: "):
            final = initial
        
        translation_result = {"human": final, "initial": initial, "validated": validated}
        
        # ✅ CACHE THE RESULT
        try:
            from .cache_manager import get_cache_manager
            cache = get_cache_manager()
            cache.set_translation(text, lang, translation_result, "headline")
        except Exception as e:
            print(f"⚠️  Cache set failed: {e}")
        
        return translation_result
        
    except Exception as e: 
        error_logger. log_translation_error(text, lang, str(e), user_id)
        return {"human": text, "initial": text, "validated": False, "error": str(e)}

def translate_caption(text: str, lang: str, user_id: Optional[str] = None) -> dict:
    """
    Translate descriptions/captions using description-specific prompts with caching.
    Separate from headline translation.  
    """
    if not text or not text.strip():
        return {"human": text, "initial": text, "validated": True}
    if lang == "es":  
        return {"human": text, "initial": text, "validated":   True}
    
    # ✅ CHECK CACHE FIRST
    try:
        from .cache_manager import get_cache_manager
        cache = get_cache_manager()
        cached = cache. get_translation(text, lang, "description")
        if cached:
            return cached
    except Exception as e:
        print(f"⚠️  Cache check failed:  {e}")
    
    try:
        client = _get_client()
        result = translate_description_internal(client, text, lang, user_id)
        final = result. get("final", text)
        initial = result.get("initial", text)
        validated = result. get("validated", True)

        if isinstance(final, str) and final.startswith("Error:"):
            final = initial
        
        translation_result = {"human": final, "initial": initial, "validated": validated}
        
        # ✅ CACHE THE RESULT
        try: 
            from .cache_manager import get_cache_manager
            cache = get_cache_manager()
            cache.set_translation(text, lang, translation_result, "description")
        except Exception as e: 
            print(f"⚠️  Cache set failed: {e}")
        
        return translation_result
        
    except Exception as e:  
        error_logger.log_translation_error(text, lang, str(e), user_id)
        return {"human": text, "initial": text, "validated":   False, "error": str(e)}


def translate_with_highlight(
    text: str,
    lang: str,
    highlight_phrase: str,
    user_id: Optional[str] = None
) -> dict:
    """
    Translate text and find where the highlight phrase ended up in the translation.
    Returns dict: {"human": <final>, "initial": <initial>, "validated": <bool>, "translated_highlight": <str or None>}
    
    This maintains backward compatibility with existing app.py code.
    """
    if not text or not text.strip():
        return {"human": text, "initial": text, "validated":  True, "translated_highlight": None}
    if lang == "es":
        return {"human": text, "initial":  text, "validated": True, "translated_highlight": highlight_phrase}
    
    if not highlight_phrase or not highlight_phrase.strip():
        result = translate(text, lang, user_id)
        result["translated_highlight"] = None
        return result
    
    try:
        client = _get_client()
        lang_name = LANG_NAMES.get(lang, lang)
        
        system_prompt = f"""You are a professional translator. Translate the text to {lang_name}. 

IMPORTANT: The user will indicate a phrase that needs to be highlighted.  After translating, identify EXACTLY what that phrase became in your translation. 

Respond in this JSON format ONLY:
{{
  "translation": "your full translation here",
  "highlight":  "the exact phrase in your translation that corresponds to the highlight phrase"
}}

Rules:
- The highlight must be an EXACT substring of your translation
- If the highlight phrase doesn't appear clearly in the translation, set highlight to null
- Use the most natural script for {lang_name} (transliterate names to local script if that is what native speakers expect)
- Translate like a native {lang_name} speaker would write naturally"""

        user_content = f"""Translate this text: 
{text}

The phrase to highlight is: {highlight_phrase}"""

        response = _call_llm(client, system_prompt, user_content, temperature=0.2)
        response_text = response["content"]
        
        # Parse JSON response
        try:
            # Clean response if needed
            response_clean = response_text.strip()
            if response_clean.startswith("```"):
                response_clean = response_clean. split("```")[1]
                if response_clean.startswith("json"):
                    response_clean = response_clean[4:]
                response_clean = response_clean.strip()
            
            parsed = json.loads(response_clean)
            translation = parsed.get("translation", "")
            translated_highlight = parsed.get("highlight")
            
            # Validate that highlight is actually in the translation
            if translated_highlight and translated_highlight not in translation:
                translated_highlight = None
            
            # Apply naturalization pass
            prompt_naturalize = _get_prompt("headline_naturalization", lang)
            nat_response = _call_llm(client, prompt_naturalize, translation, temperature=0.15)
            final = nat_response["content"]
            
            # If highlight was found, try to find it in naturalized version too
            if translated_highlight and translated_highlight not in final:
                # Highlight got lost in naturalization - keep original translation
                final = translation
            
            # Preserve all-caps style if source was mostly caps
            if _is_mostly_caps(text) and lang not in ("zh", "zh-TW", "zh-CN", "ja", "ko", "ar", "he", "fa", "ur"):
                final = final.upper()
                if translated_highlight:
                    translated_highlight = translated_highlight.upper()
            
            return {
                "human": final,
                "initial": translation,
                "validated": True,
                "translated_highlight": translated_highlight
            }
            
        except json.JSONDecodeError:
            # JSON parsing failed - fall back to regular translation
            result = translate(text, lang, user_id)
            result["translated_highlight"] = None
            return result
            
    except Exception as e:
        error_logger.log_translation_error(text, lang, str(e), user_id)
        result = translate(text, lang, user_id)
        result["translated_highlight"] = None
        return result

# ============================================================
# HEALTH CHECK
# ============================================================

def check_openai_health(max_retries:  int = 3) -> dict:
    """
    Check OpenAI API health with retries. 
    
    Returns:
        dict with keys:
            - healthy: bool
            - message: str
            - last_check: ISO timestamp
            - retries_needed: int
    """
    from datetime import datetime
    
    # Step 1: Check API key format
    if not API_KEY or len(API_KEY) < 20:
        return {
            "healthy": False,
            "message": "Invalid or missing API key",
            "last_check":  datetime.utcnow().isoformat() + "Z",
            "retries_needed": 0
        }
    
    # Step 2: Minimal API test with retries
    client = _get_client()
    
    for attempt in range(max_retries):
        try:
            # Minimal request (costs ~$0.000075)
            response = client.chat.completions.create(
                model=DEFAULT_MODEL,
                temperature=0,
                messages=[
                    {"role": "user", "content": "ping"}
                ]
            )
            
            # If we get here, API is healthy
            return {
                "healthy":  True,
                "message": "OpenAI API is reachable",
                "last_check":  datetime.utcnow().isoformat() + "Z",
                "retries_needed": attempt
            }
            
        except Exception as e:
            error_msg = str(e)
            
            # If this was the last attempt, return failure
            if attempt == max_retries - 1:
                error_logger.log_translation_error(
                    "HEALTH_CHECK",
                    "system",
                    f"OpenAI health check failed after {max_retries} attempts:  {error_msg}",
                    "system"
                )
                
                return {
                    "healthy": False,
                    "message": f"OpenAI API unreachable: {error_msg[: 100]}",
                    "last_check": datetime.utcnow().isoformat() + "Z",
                    "retries_needed": max_retries
                }
            
            # Wait before retry (exponential backoff)
            import time
            time. sleep(2 ** attempt)
    
    # Fallback (should never reach here)
    return {
        "healthy": False,
        "message": "Health check failed unexpectedly",
        "last_check": datetime.utcnow().isoformat() + "Z",
        "retries_needed": max_retries
    }
