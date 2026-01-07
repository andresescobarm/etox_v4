#!/usr/bin/env python3
from bidi.algorithm import get_display
import csv
import io
import json
import re
import zipfile
import copy
import concurrent. futures
import multiprocessing
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from typing import List, Tuple, Optional
import asyncio
from datetime import datetime

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi. responses import StreamingResponse, JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageDraw, ImageFont, features, UnidentifiedImageError

from tradufotos import translate, translate_caption, translate_with_highlight
from user_queue import get_user_queue
from cache_manager import get_cache_manager

import base64
from celery. result import AsyncResult
from celery_tasks import render_download_job


# ============================================================
# PATHS
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_PATH = BASE_DIR / "templates" / "templates.json"
FONTS_DIR = BASE_DIR / "fonts"

# ============================================================
# LIBRAQM CHECK
# ============================================================

RAQM_AVAILABLE = features.check("raqm")
if not RAQM_AVAILABLE:  
    print("⚠️  WARNING: libraqm not available.    Complex scripts may not render correctly.")
    print("   Install:    brew install libraqm fribidi harfbuzz && pip install --upgrade Pillow --no-cache-dir")
else:
    print("✅ libraqm available - complex text shaping enabled")

# ============================================================
# RTL AND COMPLEX SCRIPT DETECTION
# ============================================================

RTL_LANGUAGES = {"ar", "he", "fa", "ur"}
COMPLEX_SCRIPT_LANGUAGES = {"ar", "he", "hi", "bn", "ta", "te", "th", "ml", "kn", "gu", "pa", "my", "km", "lo"}
NO_SPACE_SCRIPTS = {"th", "lo", "my", "km"}

def is_rtl_language(lang: str) -> bool:
    return lang in RTL_LANGUAGES

def needs_complex_shaping(lang: str) -> bool:
    return lang in COMPLEX_SCRIPT_LANGUAGES

def uses_no_space_script(lang: str) -> bool:
    return lang in NO_SPACE_SCRIPTS


def process_rtl_text(text: str, lang: str) -> str:
    """
    Process text for RTL languages using the BiDi algorithm.
    This reorders the text for proper visual display.
    """
    if not is_rtl_language(lang):
        return text
    try:
        return get_display(text)
    except Exception as e:
        print(f"RTL processing error:  {e}")
        return text


# ============================================================
# FUZZY MATCHING
# ============================================================

def find_best_fuzzy_match(text, target_phrase, threshold=0.7):
    words = text.split()
    target_n = max(1, len(target_phrase.split()))
    best = None
    best_score = 0
    n = len(words)
    for i in range(n):
        for j in range(i + 1, min(n + 1, i + target_n + 4)):
            chunk = " ".join(words[i:j])
            score = SequenceMatcher(None, chunk. lower(), target_phrase.lower()).ratio()
            if score > best_score:
                best_score = score
                best = chunk
    if best_score >= threshold:
        return best
    return None

# ============================================================
# FASTAPI APP SETUP
# ============================================================

    
app = FastAPI(title="Etos Backend")

# Global health state
health_state = {
    "openai_healthy": True,
    "last_check": None,
    "message": "Not checked yet",
    "retries_needed": 0
}


# Initialize cache on startup
_ = get_cache_manager()


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Background health check task
async def periodic_health_check():
    """Run health check every 10 minutes in background."""
    from tradufotos import check_openai_health
    
    while True: 
        try:
            # Run health check
            result = check_openai_health(max_retries=3)
            
            # Update global state
            health_state["openai_healthy"] = result["healthy"]
            health_state["last_check"] = result["last_check"]
            health_state["message"] = result["message"]
            health_state["retries_needed"] = result["retries_needed"]
            
            # Log if unhealthy
            if not result["healthy"]:
                print(f"⚠️  HEALTH CHECK FAILED: {result['message']}")
            else:
                print(f"✅ Health check passed (retries: {result['retries_needed']})")
                
        except Exception as e: 
            print(f"❌ Health check error: {e}")
            health_state["openai_healthy"] = False
            health_state["message"] = f"Health check exception: {str(e)}"
        
        # Wait 10 minutes (600 seconds)
        await asyncio.sleep(600)

@app.on_event("startup")
async def startup_event():
    """Run health check on startup and start background task."""
    from tradufotos import check_openai_health
    
    print("🔍 Running initial health check...")
    result = check_openai_health(max_retries=3)
    
    health_state["openai_healthy"] = result["healthy"]
    health_state["last_check"] = result["last_check"]
    health_state["message"] = result["message"]
    health_state["retries_needed"] = result["retries_needed"]
    
    if result["healthy"]:
        print(f"✅ OpenAI API is healthy (retries: {result['retries_needed']})")
    else:
        print(f"⚠️  OpenAI API is UNHEALTHY: {result['message']}")
    
    # Start background health check task
    asyncio.create_task(periodic_health_check())
    print("🔄 Background health checks started (every 10 minutes)")

@app.get("/", include_in_schema=False)
def root_redirect():
    return RedirectResponse(url="/ui/index.html")


# ============================================================
# PUNCTUATION HANDLING
# ============================================================

_punct_re = re.compile(r"^[^\w\s]+$", flags=re.UNICODE)

def is_punctuation_token(s: str) -> bool:
    return bool(_punct_re.match(s))

OPENING_PUNCT = {'"', "«", "(", "[", "¿", "¡"}
CLOSING_PUNCT = {",", ".", ";", ":", "!", "?", '"', "»", ")", "]", "…", "—", "–", "-"}

def contains_opening_punct(s: str) -> bool:
    return bool(s) and any(ch in OPENING_PUNCT for ch in s)

def contains_closing_punct(s: str) -> bool:
    return bool(s) and any(ch in CLOSING_PUNCT for ch in s)

def is_all_caps(text:  str) -> bool:
    """Check if text is predominantly uppercase (for ALL CAPS headlines)"""
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return False
    # Consider it all caps if 90%+ of letters are uppercase
    uppercase_count = sum(1 for ch in letters if ch.isupper())
    return uppercase_count / len(letters) >= 0.9

def _spacing_between(prev_word: str, next_word: str, all_caps: bool = False) -> str:
    if not prev_word or not next_word:
        return " "
    
    prev_char = prev_word[-1]
    next_char = next_word[0]
    
    def is_cjk(ch):
        code = ord(ch)
        return (0x4e00 <= code <= 0x9fff or    # CJK
                0x3040 <= code <= 0x30ff or    # Hiragana + Katakana
                0xac00 <= code <= 0xd7af)      # Korean
  
    def is_latin(ch):
        # Check if character is Latin script (includes accented characters like Ö, Á, É)
        if ch.isascii() and ch.isalpha():
            return True
        # Check for Latin Extended characters (accented letters)
        code = ord(ch)
        return (0x00C0 <= code <= 0x024F or    # Latin Extended-A and B (Ö, Á, É, etc.)
                0x1E00 <= code <= 0x1EFF)      # Latin Extended Additional  
    
    # No space between two CJK characters
    if is_cjk(prev_char) and is_cjk(next_char):
        return ""
    
    # No space between CJK and digit
    if (is_cjk(prev_char) and next_char.isdigit()) or (prev_char.isdigit() and is_cjk(next_char)):
        return ""
    
    # No space between two digits
    if prev_char.isdigit() and next_char.isdigit():
        return ""
    
    # KEEP space between Latin characters ONLY if they look like separate words
    if is_latin(prev_char) and is_latin(next_char):
        # For ALL CAPS text, always add space between words
        if all_caps: 
            return " "
        # If both prev_word and next_word are single Latin chars, DON'T add space
        if len(prev_word) == 1 and len(next_word) == 1:
            return ""
        return " "
   
      
    # No space between CJK and Latin (they touch directly)
    if (is_cjk(prev_char) and is_latin(next_char)) or (is_latin(prev_char) and is_cjk(next_char)):
        return ""
    
    # No space between CJK and punctuation
    if is_cjk(prev_char) or is_cjk(next_char):
        return ""
    
    # Standard punctuation rules
    if is_punctuation_token(next_word):
        return ""
    if contains_opening_punct(prev_word):
        return ""
    if is_punctuation_token(prev_word):
        return " "
    
    return " "



def join_tokens_with_spacing(ln: List[Tuple[str, str]], all_caps: bool = False) -> str:
    if not ln:
        return ""
    s = ""
    for idx, (w, c) in enumerate(ln):
        s += w
        if idx < len(ln) - 1:
            next_token = ln[idx + 1][0]
            sep = _spacing_between(w, next_token, all_caps)
            s += sep
    return s


# ============================================================
# TEMPLATE LOADING
# ============================================================

def load_templates():
    if not TEMPLATES_PATH.exists():
        print("⚠️ templates/templates.json not found; returning empty templates list.")
        return []
    try:
        with TEMPLATES_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data. get("templates", [])
    except Exception as e:
        print(f"⚠️ Failed to load templates: {e}")
        return []

def get_template_by_id(template_id: str):
    templates = load_templates()
    for t in templates:
        if t. get("id") == template_id:
            return t
    available = [t. get("id") for t in templates]
    raise HTTPException(
        status_code=404,
        detail={
            "error": "Template not found",
            "requested_id": template_id,
            "available_template_ids": available,
        },
    )

@app.get("/health")
def health_check():
    """
    Health check endpoint. 
    Returns system status including OpenAI API health and queue status. 
    """
    from user_queue import get_user_queue
    
    queue = get_user_queue(max_concurrent=10)
    
    # Determine overall status
    status = "healthy" if health_state["openai_healthy"] else "unhealthy"
    http_status = 200 if health_state["openai_healthy"] else 503
    
    return JSONResponse(
        status_code=http_status,
        content={
            "status": status,
            "timestamp":  datetime.utcnow().isoformat() + "Z",
            "openai":  {
                "healthy": health_state["openai_healthy"],
                "message": health_state["message"],
                "last_check": health_state["last_check"],
                "retries_needed": health_state["retries_needed"]
            },
            "queue": {
                "max_concurrent": queue.max_concurrent,
                "active_users": len(queue.active_users),
                "available_slots": queue. max_concurrent - len(queue. active_users)
            }
        }
    )

@app.get("/templates")
def list_templates():
    return JSONResponse({"templates": load_templates()})


@app.post("/translate-concurrent")
async def translate_concurrent_endpoint(request: Request):
    """
    Concurrent translation endpoint - translates to all languages at once. 
    Faster but uses more API quota.
    
    Request body:
    {
        "text": "Text to translate",
        "user_id": "optional_user_id"
    }
    
    Returns:
    {
        "results": [... ],
        "total_time": 12.34,
        "total_cost": 0.1234
    }
    """
    try:
        data = await request.json()
        text = data.get("text", "").strip()
        user_id = data.get("user_id")
        
        if not text: 
            return JSONResponse(
                status_code=400,
                content={"error": "No text provided"}
            )
        
        # Import the concurrent function
        from tradufotos import translate_all_concurrent
        
        # Run the concurrent translation
        results = await translate_all_concurrent(text, user_id)
        
        total_time = sum(r. get("elapsed_seconds", 0) for r in results)
        total_cost = sum(r. get("estimated_cost_usd", 0) for r in results)
        
        return JSONResponse(
            status_code=200,
            content={
                "results": results,
                "total_languages": len(results),
                "total_time_seconds":  round(total_time, 2),
                "total_cost_usd": round(total_cost, 4),
                "mode": "concurrent"
            }
        )
        
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error":  str(e)}
        )


# ============================================================
# TEXT RENDERING WITH LIBRAQM SUPPORT
# ============================================================

def get_text_length_shaped(draw:  ImageDraw. Draw, text: str, font: ImageFont.FreeTypeFont) -> float:
    return draw.textlength(text, font=font)

def get_text_bbox_shaped(draw: ImageDraw.Draw, position: Tuple[int, int], text: str, font: ImageFont.FreeTypeFont) -> Tuple[int, int, int, int]: 
    return draw.textbbox(position, text, font=font)

# ============================================================
# GRAPHEME TOKENIZATION
# ============================================================

def split_graphemes(text: str) -> List[str]:
    clusters = []
    for ch in text:
        if clusters and unicodedata.combining(ch):
            clusters[-1] += ch
        else:
            clusters. append(ch)
    return clusters

# ============================================================
# TEXT WRAPPING
# ============================================================

def wrap_text(draw: ImageDraw.Draw, text: str, font: ImageFont. FreeTypeFont, max_width: int):
    has_cjk = bool(re.search(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]", text))
    if has_cjk:
        words = list(text)
    else:
        words = text.split()

    if not words:
        return [""]

    lines = []
    cur = words[0]
    for w in words[1:]:
        sep = "" if has_cjk else " "
        test = cur + sep + w
        if get_text_length_shaped(draw, test, font) <= max_width:
            cur = test
        else:
            lines.append(cur)
            cur = w
    lines.append(cur)
    return lines

def wrap_words_with_color(
    draw: ImageDraw.Draw, words_with_color: List[Tuple[str, str]], font: ImageFont.FreeTypeFont, max_width: int, all_caps: bool = False
):
    if not words_with_color:  
        return [[]]

    # Detect CJK in any token
    has_cjk = any(re.search(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]", w) for w, _ in words_with_color)

    # If CJK, expand CJK characters to per-character tokens, but keep Latin words intact
    if has_cjk:
        expanded = []
        for w, col in words_with_color: 
            # Check if this word contains CJK
            if re.search(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]", w):
                # Split CJK words into individual characters
                for ch in w:
                    expanded.append((ch, col))
            else:
                # Keep Latin words intact
                expanded.append((w, col))
        words_with_color = expanded
    

    def line_text(tokens):
        # Build text using the same spacing rule as join_tokens_with_spacing
        parts = []
        for idx, (w, _) in enumerate(tokens):
            parts.append(w)
            if idx < len(tokens) - 1:
                prev = w
                nxt = tokens[idx + 1][0]
                parts.append(_spacing_between(prev, nxt, all_caps))
        return "".join(parts)

    lines = []
    cur_line = [words_with_color[0]]

    for wcol in words_with_color[1:]:
        candidate = cur_line + [wcol]
        cand_text = line_text(candidate)
        if get_text_length_shaped(draw, cand_text, font) <= max_width:
            cur_line. append(wcol)
        else:
            lines.append(cur_line)
            cur_line = [wcol]

    lines.append(cur_line)
    return lines


# Helpers for spans/highlight

def split_spans_to_words(spans: List[dict], default_color: str):
    words = []
    token_re = re.compile(r"[^\s]+", flags=re.UNICODE)
    for span in spans:
        text = span. get("text", "")
        color = span.get("color", default_color)
        for m in token_re.finditer(text):
            token = m.group(0)
            words.append((token, color))
    return words

def build_spans_from_highlight_phrase(text: str, base_color: str, highlight_color: str, phrase: str):
    if not phrase:
        return None
    raw_phrases = [p.strip() for p in phrase.split("/") if p.strip()]
    if not raw_phrases:
        return None
    flags = re.IGNORECASE | re.UNICODE
    matches = []
    for p in raw_phrases:
        pattern = re.compile(re.escape(p), flags=flags)
        for m in pattern.finditer(text):
            matches.append((m.start(), m.end(), m.group(0)))
    if not matches:
        return [{"text": text, "color":  base_color}]
    matches.sort(key=lambda x: (x[0], -(x[1] - x[0])))
    merged = []
    cur_idx = 0
    for start, end, _ in matches:
        if end <= cur_idx:
            continue
        if start < cur_idx:
            start = cur_idx
        if start > cur_idx:
            merged.append((cur_idx, start, False))
        merged.append((start, end, True))
        cur_idx = end
    if cur_idx < len(text):
        merged.append((cur_idx, len(text), False))
    spans:  List[dict] = []
    for s, e, is_high in merged:
        seg = text[s:e]
        if not seg:
            continue
        spans. append({"text": seg, "color":  highlight_color if is_high else base_color})
    if not spans:
        return [{"text": text, "color":  base_color}]
    return spans

# ============================================================
# LAYOUT CALCULATIONS
# ============================================================

def compute_effective_gap(line_h: int, line_spacing: int, mode: str = "legacy"):
    if mode == "add":
        effective_gap = line_h + int(line_spacing)
    else:  
        effective_gap = line_h - int(line_spacing)
    min_gap = max(int(line_h * 0.1), 0)
    if effective_gap < min_gap:  
        effective_gap = min_gap
    return effective_gap

# ============================================================
# FIT + WRAP
# ============================================================

def ellipsize_plain_last_line(draw: ImageDraw. Draw, font: ImageFont.FreeTypeFont, line: str, max_width: int, ellipsis: str = "..."):
    if get_text_length_shaped(draw, line, font) <= max_width:
        return line
    words = line.split()
    if not words:
        return ellipsis if get_text_length_shaped(draw, ellipsis, font) <= max_width else ""
    while words:  
        candidate = " ".join(words) + ellipsis
        if get_text_length_shaped(draw, candidate, font) <= max_width:
            return candidate
        words. pop()
    return ellipsis if get_text_length_shaped(draw, ellipsis, font) <= max_width else ""

def ellipsize_spans_last_line(draw: ImageDraw.Draw, font: ImageFont.FreeTypeFont, line_words: List[Tuple[str, str]], max_width: int, ellipsis:  str = ".. .", all_caps: bool = False):
    while line_words:
        text = join_tokens_with_spacing(line_words, all_caps)
        if get_text_length_shaped(draw, text + ellipsis, font) <= max_width:
            return line_words + [(ellipsis, line_words[-1][1])], line_words[-1][1]
        line_words = line_words[:-1]
    if get_text_length_shaped(draw, ellipsis, font) <= max_width:
        return [], None
    return [], None

def fit_text_in_box(
    draw,
    text:  str,
    font_path:  Path,
    max_font_size: int,
    min_font_size: int,
    box_w: int,
    box_h: int,
    max_lines: int,
    line_spacing: int = 0,
    line_spacing_mode: str = "legacy",
    spans: List[dict] = None,
    default_color: str = "#000000",
    preferred_font_size: Optional[int] = None,
    font_size_mode: str = "auto",
    min_line_gap_pct: float = 0.10,
    max_line_gap_pct: float = 0.18,
    area_id: str = "",
    template_id: str = "",
    all_caps: bool = False
):

    """
    Returns (lines, font, line_step, total_h)
    line_step is the max measured line height used for spacing.  
    """
    pad_factor = 0.03
    min_gap_px = 1

    def measure_lines(lines_local, font_local, spans_local=False):
        max_h = 0
        for ln in lines_local:
            line_text = join_tokens_with_spacing(ln, all_caps) if spans_local else (ln if ln else " ")
            bbox = draw.textbbox((0, 0), line_text, font=font_local)
            h = int(bbox[3] - bbox[1])
            if h > max_h:
                max_h = h
        if max_h == 0:
            ascent, descent = font_local.getmetrics()
            max_h = ascent + descent
        return max_h

    def clamp_gap(line_step_val, eff_gap_val):
        eff_gap_val = max(eff_gap_val, int(line_step_val * min_line_gap_pct))
        eff_gap_val = min(eff_gap_val, int(line_step_val * max_line_gap_pct))
        eff_gap_val = max(eff_gap_val, min_gap_px)
        return eff_gap_val

    if preferred_font_size is not None and font_size_mode == "absolute":
        font_size = int(preferred_font_size)
    else:
        font_size = min(int(preferred_font_size), max_font_size) if preferred_font_size is not None else max_font_size

    font = ImageFont.truetype(str(font_path), size=font_size)

    if font_size_mode == "absolute" and preferred_font_size is not None:
        if spans:  
            words_with_color = split_spans_to_words(spans, default_color)
            lines = wrap_words_with_color(draw, words_with_color, font, box_w, all_caps)
        else:
            lines = wrap_text(draw, text, font, box_w)
        line_step = measure_lines(lines, font, spans is not None)
        eff_gap = clamp_gap(line_step, compute_effective_gap(line_step, line_spacing, line_spacing_mode))
        # MANUAL OVERRIDE: Force tighter spacing for Template A and A.1
        if area_id == "main_box" and template_id in ["template_A_portrait_1080x1350", "template_A1_portrait_1080x1350"]:
            old_gap = eff_gap
            eff_gap = 0  # Force NO gap between lines    
            print(f"🔥 OVERRIDE TRIGGERED!  line_step={line_step}, old eff_gap={old_gap}, new eff_gap={eff_gap}")
        total_h = line_step * len(lines) + eff_gap * max(0, len(lines) - 1)
        total_h += int(line_step * pad_factor)
        return lines, font, line_step, total_h

    while font_size >= min_font_size:
        font = ImageFont.truetype(str(font_path), size=font_size)
        if spans:
            words_with_color = split_spans_to_words(spans, default_color)
            lines = wrap_words_with_color(draw, words_with_color, font, box_w, all_caps)
            num_lines = len(lines)
        else:
            lines = wrap_text(draw, text, font, box_w)
            num_lines = len(lines)

        line_step = measure_lines(lines, font, spans is not None)
        eff_gap = clamp_gap(line_step, compute_effective_gap(line_step, line_spacing, line_spacing_mode))
        # MANUAL OVERRIDE: Force tighter spacing for Template A and A.1
        if area_id == "main_box" and template_id in ["template_A_portrait_1080x1350", "template_A1_portrait_1080x1350"]:
            old_gap = eff_gap
            eff_gap = 0  # Force NO gap between lines
            print(f"🔥 OVERRIDE TRIGGERED!  line_step={line_step}, old eff_gap={old_gap}, new eff_gap={eff_gap}")
        total_h = line_step * num_lines + eff_gap * max(0, num_lines - 1)
        total_h += int(line_step * pad_factor)

        if num_lines <= max_lines and total_h <= box_h:
            return lines, font, line_step, total_h

        font_size -= 2
        if font_size < min_font_size:
            break

    # Fallback at min font size
    font = ImageFont. truetype(str(font_path), size=min_font_size)
    if spans:
        words_with_color = split_spans_to_words(spans, default_color)
        lines = wrap_words_with_color(draw, words_with_color, font, box_w, all_caps)
        if len(lines) > max_lines:
            truncated = lines[:max_lines]
            last_line_words = truncated[-1]
            new_last_words, _ = ellipsize_spans_last_line(draw, font, last_line_words, box_w, ellipsis=".. .", all_caps=all_caps)
            truncated[-1] = new_last_words
            lines = truncated
    else:
        lines = wrap_text(draw, text, font, box_w)
        if len(lines) > max_lines:
            truncated = lines[:max_lines]
            last_line = truncated[-1]
            new_last_line = ellipsize_plain_last_line(draw, font, last_line, box_w, ellipsis="...")
            truncated[-1] = new_last_line
            lines = truncated

    line_step = measure_lines(lines, font, spans is not None)
    eff_gap = clamp_gap(line_step, compute_effective_gap(line_step, line_spacing, line_spacing_mode))
    # MANUAL OVERRIDE: Force tighter spacing for Template A and A.1
    if area_id == "main_box" and template_id in ["template_A_portrait_1080x1350", "template_A1_portrait_1080x1350"]:
        eff_gap = 0  # Force NO gap between lines
    total_h = line_step * len(lines) + eff_gap * max(0, len(lines) - 1)
    total_h += int(line_step * pad_factor)
    return lines, font, line_step, total_h

# ============================================================
# BBOX HELPER
# ============================================================

def compute_bbox_pixels(template, bbox):
    W = template["size"]["width"]
    H = template["size"]["height"]
    x = int(round(bbox["x_pct"] * W))
    y = int(round(bbox["y_pct"] * H))
    w = int(round(bbox["w_pct"] * W))
    h = int(round(bbox["h_pct"] * H))
    return x, y, w, h

# ============================================================
# MAIN CANVAS RENDERING
# ============================================================


def render_canvas(template, payload_data, file_bytes):
    try:
        user_img = Image. open(io.BytesIO(file_bytes)).convert("RGBA")
    except (PIL.UnidentifiedImageError, ValueError, IOError) as e:
        raise HTTPException(
            status_code=400, 
            detail=f"Invalid image file: {str(e)[: 100]}"
        )

    target_w = template["size"]["width"]
    target_h = template["size"]["height"]

    src_w, src_h = user_img.size
    src_ratio = src_w / src_h if src_h != 0 else 1.0
    target_ratio = target_w / target_h if target_h != 0 else 1.0

    if src_ratio > target_ratio:  
        new_h = target_h
        new_w = int(round(target_h * src_ratio))
    else: 
        new_w = target_w
        new_h = int(round(target_w / src_ratio))

    user_resized = user_img.resize((new_w, new_h), Image.LANCZOS)

    left = (new_w - target_w) // 2
    top = (new_h - target_h) // 2
    right = left + target_w
    bottom = top + target_h

    canvas = user_resized.crop((left, top, right, bottom))

    if canvas.mode != "RGBA":  
        canvas = canvas.convert("RGBA")

    draw = ImageDraw.Draw(canvas)

    MAX_BITMAP_UPSCALE = 1.9

    for entry in payload_data:
        area_id = entry. get("area_id")
        text = entry.get("text", "")
        # Check if all_caps is forced via payload, otherwise auto-detect
        
        text_all_caps = entry.get("all_caps", is_all_caps(text))
        # If all_caps is True, convert the text to uppercase
        if text_all_caps and text:
            text = text.upper()
        spans = entry.get("spans", None)
        # Uppercase spans if all_caps is enabled
        if text_all_caps and spans:
            spans = [{"text": s.get("text", "").upper(), "color": s.get("color")} for s in spans]
        lang = entry.get("lang", "default")
        color = entry.get("color", template. get("allowed_colors", ["#000000"])[0])
        highlight_color = entry.get("highlight_color", None)
        
        highlight_phrase = entry.get("highlight_phrase", None)
        # Uppercase highlight_phrase if all_caps is enabled
        if text_all_caps and highlight_phrase:
            highlight_phrase = highlight_phrase.upper()
        font_size_hint_raw = entry.get("font_size", None)
        preferred_font:  Optional[int] = None
        if font_size_hint_raw is not None:
            try:
                preferred_font = int(font_size_hint_raw)
            except Exception:
                preferred_font = None
        font_size_mode_raw = entry.get("font_size_mode", None)
        if font_size_mode_raw in ("absolute", "auto"):
            font_size_mode = font_size_mode_raw
        elif preferred_font is not None:
            font_size_mode = "absolute"
        else:
            font_size_mode = "auto"

        areas = [a for a in template.get("text_areas", []) if a["id"] == area_id]
        if not areas:
            continue
        area = areas[0]
        bx, by, bw, bh = compute_bbox_pixels(template, area["bbox"])

        entry_bg = entry.get("background_color")
        if entry_bg:  
            try:
                rgba = tuple(int(entry_bg. lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)) + (255,)
                draw.rectangle([bx, by, bx + bw, by + bh], fill=rgba)
            except Exception:
                pass

        font_key = lang if lang in template.get("font_map", {}) else "default"
        font_filename = template.get("font_map", {}).get(font_key, template. get("font_map", {}).get("default"))
        font_path = FONTS_DIR / font_filename
        if not font_path.exists():
            raise HTTPException(status_code=500, detail=f"Font file missing: {font_path}")

        max_fs = area. get("max_font_size", 200)
        min_fs = area.get("min_font_size", 12)
        max_lines = area.get("max_lines", 3)
        line_spacing = int(area.get("line_spacing", 0))
        line_spacing_mode = area.get("line_spacing_mode", "legacy")
        min_line_gap_pct = float(area.get("min_line_gap_pct", 0.10))
        max_line_gap_pct = float(area.get("max_line_gap_pct", 0.18))

        # Process RTL text if needed
        if is_rtl_language(lang):
            text = process_rtl_text(text, lang)
            if highlight_phrase:
                highlight_phrase = process_rtl_text(highlight_phrase, lang)

        used_spans = None
        if spans:
            # Process RTL for each span
            if is_rtl_language(lang):
                used_spans = []
                for span in spans:  
                    new_span = span.copy()
                    new_span["text"] = process_rtl_text(span. get("text", ""), lang)
                    used_spans.append(new_span)
            else:
                used_spans = spans
        elif highlight_color and highlight_phrase:
            used_spans = build_spans_from_highlight_phrase(text, color, highlight_color, highlight_phrase)
        
        lines, font, line_step, total_h = fit_text_in_box(
            draw,
            text if not used_spans else None,
            font_path,
            max_fs,
            min_fs,
            bw,
            bh,
            max_lines,
            line_spacing,
            line_spacing_mode=line_spacing_mode,
            spans=used_spans,
            default_color=color,
            preferred_font_size=preferred_font,
            font_size_mode=font_size_mode,
            min_line_gap_pct=min_line_gap_pct,
            max_line_gap_pct=max_line_gap_pct,
            area_id=area_id,
            template_id=template["id"],
            all_caps=text_all_caps
        )


        measured_line_texts = []
        measured_line_heights = []
        for ln in lines:
            line_text = join_tokens_with_spacing(ln, text_all_caps) if used_spans else (ln if ln else " ")
            # If the language is RTL, reorder visually for proper rendering/measurement
            display_line_text = get_display(line_text) if is_rtl_language(lang) else line_text
            measured_line_texts.append(display_line_text)
            bbox = get_text_bbox_shaped(draw, (0, 0), display_line_text if display_line_text != "" else " ", font)
            h = int(bbox[3] - bbox[1])
            measured_line_heights.append(h)
        else:
            ascent, descent = font.getmetrics()
            line_step = ascent + descent

        eff_gap = compute_effective_gap(line_step, line_spacing, line_spacing_mode)
        eff_gap = max(eff_gap, int(line_step * min_line_gap_pct))
        eff_gap = min(eff_gap, int(line_step * max_line_gap_pct))
        block_pad = int(line_step * 0.03)
        block_h = block_pad * 2 + line_step * len(lines) + eff_gap * max(0, len(lines) - 1)
        block_h = max(1, int(block_h))
        block_w = bw
        block = Image.new("RGBA", (block_w, block_h), (0, 0, 0, 0))
        bd = ImageDraw.Draw(block)

        y_cursor = block_pad
        for i, ln in enumerate(lines):
            line_text = measured_line_texts[i]
            line_w = bd.textlength(line_text, font=font)
            align = area.get("align", "center")
            if align == "center": 
                x = (block_w - line_w) // 2
            elif align == "left":
                x = 0
            else:
                x = block_w - line_w
            #bbox = bd.textbbox((0, 0), line_text if line_text != "" else " ", font=font)
            # Use consistent baseline for all lines
            y_top = int(y_cursor)

            if used_spans:
                cur_x = x
                for idx, (w, col) in enumerate(ln):
                    sep = _spacing_between(w, ln[idx + 1][0]) if idx < len(ln) - 1 else ""
                    print(f"🐛 SPAN:  '{w}' (len={len(w)}) + sep='{sep}' -> next:  '{ln[idx + 1][0] if idx < len(ln) - 1 else 'NONE'}'")
                    draw_text = w + sep
                    bd.text((cur_x, y_top), draw_text, font=font, fill=col)
                    cur_x += bd.textlength(draw_text, font=font)
            
            else:
                # For RTL, draw the visually ordered text
                bd.text((x, y_top), measured_line_texts[i], font=font, fill=color)

            if i < len(lines) - 1:
                y_cursor += line_step + eff_gap
            else:
                y_cursor += line_step

        bbox_non_empty = block.getbbox()
        if bbox_non_empty:
            crop = block.crop(bbox_non_empty)
        else:
            crop = block

        dx_pct = float(entry.get("dx_pct", 0.0))
        dy_pct = float(entry.get("dy_pct", 0.0))
        ox = int(dx_pct * bw)
        oy = int(dy_pct * bh)

        final_w, final_h = crop.size
        if final_h > 0 and final_w > 0:
            if final_h > bh or final_w > bw:
                scale = min(bh / final_h, bw / final_w, MAX_BITMAP_UPSCALE)
                new_w = max(1, int(final_w * scale))
                new_h = max(1, int(final_h * scale))
                crop = crop.resize((new_w, new_h), Image.LANCZOS)
                final_w, final_h = new_w, new_h

            # Horizontal alignment (already centered, keep as is)
            x_pos = bx + (bw - final_w) // 2 + ox
            
            # Vertical alignment based on v_align from template
            v_align = area.get("v_align", "middle")
            if v_align == "top":
                y_pos = by + oy
            elif v_align == "bottom":  
                y_pos = by + bh - final_h + oy
            else:  # "middle" or default
                y_pos = by + (bh - final_h) // 2 + oy

            canvas.alpha_composite(crop, dest=(x_pos, y_pos))
            

    return canvas, [], False

# ============================================================
# ROUTES FOR RENDERING
# ============================================================

@app.post("/render-preview")
async def render_preview(
    template_id: str = Form(None),
    payload: str = Form(None),
    image: UploadFile = File(None)
):
    print(f"DEBUG: template_id = {template_id}")
    print(f"DEBUG: payload = {payload}")
    print(f"DEBUG: image = {image. filename if image else 'None'}")

    if not template_id:  
        raise HTTPException(status_code=422, detail="Missing template_id")
    if not payload: 
        raise HTTPException(status_code=422, detail="Missing payload")
    if not image:
        raise HTTPException(status_code=422, detail="Missing image")

    template = get_template_by_id(template_id)
    try:
        payload_data = json.loads(payload)
        if not isinstance(payload_data, list):
            raise ValueError("payload must be a list of entries")
    except Exception as e:  
        raise HTTPException(status_code=422, detail=f"Invalid payload:  {e}")

    file_bytes = await image.read()

    # Check file size
    MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB
    if len(file_bytes) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=413,
            detail="File too large. Maximum file size is 10MB."
        )

    canvas, diagnostics, any_debug = render_canvas(template, payload_data, file_bytes)
    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    buf.seek(0)
    return StreamingResponse(buf, media_type="image/png")

# ============================================================
# BATCH DOWNLOAD WITH TRANSLATIONS
# ============================================================

def extract_important_words_from_original(text:  str) -> List[str]:
    """
    Extract proper names and numbers from the ORIGINAL text (before translation).
    These will be searched for in the translated text.
    """
    important_words = []
    
    # Find numbers (years, quantities, etc.)
    number_pattern = re.compile(r'\b\d{1,4}\b')
    for match in number_pattern.finditer(text):
        important_words.append(match.group(0))
    
    # Find capitalized word sequences (proper names like "Avril Lavigne", "Banshee Complicated")
    # This pattern finds sequences of capitalized words
    words = text.split()
    i = 0
    while i < len(words):
        word = words[i]
        # Check if word starts with capital and isn't first word of sentence
        # Also skip very short words and common Spanish words
        skip_words = {'El', 'La', 'Los', 'Las', 'Un', 'Una', 'De', 'Del', 'En', 'Por', 'Para', 'Con', 'Su', 'Sus'}
        
        if (len(word) >= 2 and 
            word[0].isupper() and 
            word not in skip_words and
            not word.isupper()):  # Skip all-caps words like "USA"
            
            # Check if this starts a multi-word proper name
            name_parts = [word. strip('.,;:!?  ¿¡"\'')]
            j = i + 1
            
            while j < len(words):
                next_word = words[j]. strip('.,;:!? ¿¡"\'')
                # Continue if next word is also capitalized (part of name)
                if (len(next_word) >= 2 and 
                    next_word[0].isupper() and 
                    next_word not in skip_words and
                    not next_word.isupper()):
                    name_parts.append(next_word)
                    j += 1
                else:  
                    break
            
            full_name = ' '.join(name_parts)
            if len(full_name) >= 3 and full_name not in important_words:
                important_words.append(full_name)
            
            i = j  # Skip past the words we've processed
        else:  
            i += 1
    
    # Find text in quotes
    quote_patterns = [
        re.compile(r'"([^"]+)"'),
        re.compile(r'"([^"]+)"'),
        re.compile(r'«([^»]+)»'),
    ]
    for pattern in quote_patterns:
        for match in pattern.finditer(text):
            quoted = match.group(1).strip()
            if quoted and quoted not in important_words:
                important_words.append(quoted)
    
    return important_words


def find_important_words_in_translation(translated_text: str, important_words: List[str], highlight_color: str, base_color: str) -> List[dict]:
    """
    Search for the important words (from original) in the translated text.
    Returns spans with highlighting, or None if nothing found.
    """
    found_matches = []
    
    for word in important_words:
        # Search for exact match (case-insensitive)
        pattern = re.compile(re.escape(word), flags=re.IGNORECASE)
        for match in pattern.finditer(translated_text):
            found_matches.append((match.start(), match.end(), match.group(0)))
    
    if not found_matches:
        return None
    
    # Sort by position and remove overlaps
    found_matches.sort(key=lambda x: (x[0], -(x[1] - x[0])))
    
    merged = []
    cur_end = 0
    for start, end, text in found_matches:
        if start >= cur_end:
            merged. append((start, end, text))
            cur_end = end
    
    if not merged:
        return None
    
    # Build spans
    spans = []
    last_end = 0
    
    for start, end, match_text in merged:
        # Add non-highlighted text before this match
        if start > last_end:
            spans.append({"text": translated_text[last_end:start], "color": base_color})
        
        # Add highlighted match
        spans.append({"text": match_text, "color": highlight_color})
        last_end = end
    
    # Add remaining text
    if last_end < len(translated_text):
        spans.append({"text": translated_text[last_end:], "color": base_color})
    
    return spans if spans else None


@app.get("/cache-stats")
def get_cache_stats():
    """
    Get Redis cache statistics.
    Useful for monitoring cache hit rate.
    """
    try:
        from cache_manager import get_cache_manager
        cache = get_cache_manager()
        stats = cache.get_stats()
        
        return JSONResponse(
            status_code=200,
            content={
                "cache":  stats,
                "timestamp": datetime.utcnow().isoformat() + "Z"
            }
        )
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": str(e)}
        )

@app.post("/render-download")
async def render_download(
    request: Request,
    template_id: str = Form(... ),
    payload: str = Form(...),
    image: UploadFile = File(... ),
    languages: str = Form("[]"),
    base_name: str = Form("image"),
    permanent_note: str = Form("")
):
    """
    Renders the image with translations for multiple languages and returns a ZIP file.
    - Always includes the original Spanish (es) version
    - Translates text to each selected language
    - Returns a ZIP containing all rendered images
    """
    
    # Get user IP address as identifier
    user_ip = request.client.host if request.client else "unknown"
    
    # Get global queue instance
    queue = get_user_queue(max_concurrent=10)
    
    
    # Acquire slot (will wait if 10 users already processing)
    await queue.acquire(user_ip)
    
    # Check OpenAI health before processing
    if not health_state["openai_healthy"]: 
        queue.release(user_ip)
        raise HTTPException(
            status_code=503,
            detail={
                "error": "OpenAI API is currently unavailable",
                "message": health_state["message"],
                "last_check": health_state["last_check"]
            }
        )
    
    try:
        # Parse inputs
        template = get_template_by_id(template_id)
    
        try:
            payload_data = json.loads(payload)
            if not isinstance(payload_data, list):
                raise ValueError("payload must be a list of entries")
        except Exception as e:
            raise HTTPException(status_code=422, detail=f"Invalid payload:  {e}")
        
        try:
            selected_languages = json.loads(languages)
            if not isinstance(selected_languages, list):
                selected_languages = []
        except Exception:  
            selected_languages = []
        
        # Read image bytes once
        file_bytes = await image.read()

        # ✅ ADD THIS CHECK HERE:
        MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB
        if len(file_bytes) > MAX_FILE_SIZE:
            raise HTTPException(
                status_code=413,
                detail="File too large. Maximum file size is 10MB."
            )
        
        # Ensure Spanish is always included and is first
        all_languages = ["es"]
        for lang in selected_languages:
            if lang != "es" and lang not in all_languages: 
                all_languages.append(lang)
        
        # Pre-extract important words from original Spanish text for fallback highlighting
        original_important_words = {}
        for idx, entry in enumerate(payload_data):
            original_text = entry.get("text", "")
            if original_text. strip():
                important_words = extract_important_words_from_original(original_text)
                if important_words:  
                    original_important_words[idx] = important_words
                    print(f"DEBUG: Extracted important words from entry {idx}: {important_words}")
        
        # Create ZIP in memory
        zip_buffer = io.BytesIO()
        
        def render_for_language(lang:  str, original_payload: List[dict], file_bytes: bytes) -> Tuple[str, bytes]:  
            """Render image for a specific language, translating text if needed."""
            
            # Deep copy payload to avoid mutation
            lang_payload = copy.deepcopy(original_payload)
            
            # Translate text if not Spanish
            if lang != "es":  
                for idx, entry in enumerate(lang_payload):
                    original_text = entry.get("text", "")
                    highlight_color = entry.get("highlight_color", "")
                    base_color = entry.get("color", "#FFFFFF")
                    

                    if original_text.strip():
                        try:  
                            highlight_phrase = entry.get("highlight_phrase", "")
                            highlight_matched = False
                            
                            # Use smart translation if we have a highlight phrase
                            if highlight_phrase. strip():
                                result = translate_with_highlight(original_text, lang, highlight_phrase)
                                translated_text = result. get("human", original_text)
                                entry["text"] = translated_text
                                
                                # Get the highlight phrase location from the LLM
                                translated_highlight = result.get("translated_highlight")
                                
                                if translated_highlight: 
                                    entry["highlight_phrase"] = translated_highlight
                                    highlight_matched = True
                                else:  
                                    # LLM couldn't identify highlight - try fuzzy match as backup
                                    highlight_result = translate(highlight_phrase, lang)
                                    translated_phrase = highlight_result.get("human", highlight_phrase)
                                    match = find_best_fuzzy_match(translated_text, translated_phrase, threshold=0.7)
                                    if match:
                                        entry["highlight_phrase"] = match
                                        highlight_matched = True
                                    else:  
                                        entry["highlight_phrase"] = ""
                                        highlight_matched = False
                            else:
                                # No highlight phrase - use regular translation
                                result = translate(original_text, lang)
                                translated_text = result.get("human", original_text)
                                entry["text"] = translated_text

                    
                            
                            # FALLBACK: If no highlight matched and we have a highlight color,
                            # search for important words from the original text
                            if not highlight_matched and highlight_color and idx in original_important_words:
                                fallback_spans = find_important_words_in_translation(
                                    translated_text,
                                    original_important_words[idx],
                                    highlight_color,
                                    base_color
                                )
                                
                                if fallback_spans:  
                                    entry["spans"] = fallback_spans
                                    entry["highlight_phrase"] = ""  # Clear since we're using spans
                                    print(f"DEBUG: Fallback highlighting for {lang}, entry {idx}: found matches")
                            
                        except Exception as e:
                            print(f"Translation error for lang={lang}: {e}")
                            # Keep original text on translation failure
                    
                    # Set language for proper font selection
                    entry["lang"] = lang


                    # Override colors for all translations (non-Spanish)
                    entry["color"] = "#FFFFFF"  # White base color
                    entry["highlight_color"] = "#FFFF02"  # Yellow highlight

            # ============== DEBUG LINE ==============
            print(f"DEBUG lang={lang} payload={lang_payload}")
            # =========================================
            
            # Render the canvas
            canvas, _, _ = render_canvas(template, lang_payload, file_bytes)
            
            # Convert to PNG bytes
            img_buffer = io.BytesIO()
            canvas.save(img_buffer, format="PNG")
            img_buffer.seek(0)
            
            filename = f"{base_name}_{lang}.png"
            return filename, img_buffer.getvalue()
        
        # Process languages (can be parallelized for better performance)
        rendered_images = []
        
        # Use ThreadPoolExecutor for parallel rendering
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(all_languages), 4)) as executor:
            future_to_lang = {
                executor.submit(render_for_language, lang, payload_data, file_bytes): lang
                for lang in all_languages
            }
            
            for future in concurrent.futures.as_completed(future_to_lang):
                lang = future_to_lang[future]
                try: 
                    filename, img_bytes = future.result()
                    rendered_images.append((filename, img_bytes))
                except Exception as e:
                    print(f"Error rendering for language {lang}: {e}")
                    # Continue with other languages even if one fails
        
        # Sort images to ensure consistent order (es first, then alphabetically)
        def sort_key(item):
            filename = item[0]
            if "_es.png" in filename:
                return (0, filename)
            return (1, filename)
        
        rendered_images.sort(key=sort_key)
        
        # Create ZIP file with TRANSLATED descriptions
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for filename, img_bytes in rendered_images:  
                zf. writestr(filename, img_bytes)
            
            # ✅ ADD TRANSLATED DESCRIPTIONS (one per language)
            if permanent_note.strip():
                for lang in all_languages:
                    if lang == "es":
                        # Keep original Spanish description
                        description_text = permanent_note
                    else: 
                        # Translate description to target language
                        try: 
                            result = translate_caption(permanent_note, lang, user_ip)
                            description_text = result. get("human", permanent_note)
                        except Exception as e: 
                            print(f"Error translating description for {lang}: {e}")
                            description_text = permanent_note  # Fallback to Spanish
    
                    # Save description file for this language as CSV
                    if lang == "es": 
                        # Spanish file only has one column
                        csv_content = f"Spanish\n{permanent_note}"
                    else:
                        # Other languages have Spanish + Translation
                        csv_content = f"Spanish,{lang. upper()}\n{permanent_note},{description_text}"
        
                    zf.writestr(
                        f"{base_name}_{lang}_description.csv",
                        csv_content.encode("utf-8")
                    )
       
                
        zip_buffer.seek(0)
            
        # Return ZIP file as streaming response
        return StreamingResponse(
            zip_buffer,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{base_name}.zip"',
                "Content-Length": str(zip_buffer.getbuffer().nbytes)
            }
        )
    
    finally:
        # Always release the queue slot
        queue.release(user_ip)

@app.post("/render-download-async")
async def render_download_async(
    request: Request,
    template_id: str = Form(... ),
    payload: str = Form(...),
    image:  UploadFile = File(... ),
    languages: str = Form("[]"),
    base_name: str = Form("image"),
    permanent_note: str = Form("")
):
    """
    Submit a background job for rendering images with translations.
    Returns immediately with a job ID.
    
    Returns:  
        {
            "job_id": "...",
            "status_url": "/job-status/{job_id}",
            "download_url": "/download-result/{job_id}"
        }
    """
    
    # Get user IP address as identifier
    user_ip = request.client.host if request.client else "unknown"
    
    # Check OpenAI health before processing
    if not health_state["openai_healthy"]: 
        raise HTTPException(
            status_code=503,
            detail={
                "error": "OpenAI API is currently unavailable",
                "message":   health_state["message"],
                "last_check": health_state["last_check"]
            }
        )
    
    try:
        # Validate template exists
        template = get_template_by_id(template_id)
        
        # Validate payload
        try:
            payload_data = json.loads(payload)
            if not isinstance(payload_data, list):
                raise ValueError("payload must be a list of entries")
        except Exception as e: 
            raise HTTPException(status_code=422, detail=f"Invalid payload:  {e}")
        
        # Validate languages
        try: 
            selected_languages = json.loads(languages)
            if not isinstance(selected_languages, list):
                selected_languages = []
        except Exception:  
            selected_languages = []
        
        # Read and validate image
        file_bytes = await image. read()
        MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB
        if len(file_bytes) > MAX_FILE_SIZE:
            raise HTTPException(
                status_code=413,
                detail="File too large. Maximum file size is 10MB."
            )
        
        # Encode image as base64 for Celery
        file_bytes_b64 = base64.b64encode(file_bytes).decode("utf-8")
        
        # Submit job to Celery
        job = render_download_job.apply_async(
            args=[
                template_id,
                payload,
                file_bytes_b64,
                selected_languages,
                base_name,
                permanent_note,
                user_ip
            ]
        )
        
        return JSONResponse(
            status_code=202,
            content={
                "job_id": job.id,
                "status":   "submitted",
                "status_url": f"/job-status/{job.id}",
                "download_url":   f"/download-result/{job.id}"
            }
        )
        
    except HTTPException:  
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={"error": str(e)}
        )


@app.get("/job-status/{job_id}")
async def get_job_status(job_id: str):
    """
    Check the status of a background job.
    
    Returns:
        {
            "job_id": "...",
            "state": "PENDING|PROCESSING|SUCCESS|FAILURE",
            "progress": 0-100,
            "status":   "Human-readable status",
            "result": {... } (only if SUCCESS)
        }
    """
    job = AsyncResult(job_id, app=render_download_job. app)
    
    response = {
        "job_id": job_id,
        "state": job.state,
    }
    
    if job. state == "PENDING":
        response["progress"] = 0
        response["status"] = "Waiting in queue..."
    
    elif job.state == "PROCESSING":
        info = job.info or {}
        response["progress"] = info.get("progress", 0)
        response["status"] = info.get("status", "Processing...")
    
    elif job.state == "SUCCESS":
        response["progress"] = 100
        response["status"] = "Complete"
        response["result"] = job.result
    
    elif job.state == "FAILURE":  
        response["progress"] = 0
        response["status"] = "Failed"
        response["error"] = str(job.info)
    
    else:
        response["progress"] = 0
        response["status"] = job.state
    
    return JSONResponse(response)


@app.get("/download-result/{job_id}")
async def download_result(job_id: str):
    """
    Download the ZIP file from a completed job.
    """
    job = AsyncResult(job_id, app=render_download_job.app)
    
    if job.state != "SUCCESS":
        raise HTTPException(
            status_code=400,
            detail=f"Job not ready. Current state: {job.state}"
        )
    
    result = job.result
    
    if not result. get("success"):
        raise HTTPException(
            status_code=500,
            detail=result.get("error", "Job failed")
        )
    
    # Decode base64 ZIP
    zip_bytes_b64 = result["zip_bytes_b64"]
    zip_bytes = base64.b64decode(zip_bytes_b64)
    
    zip_buffer = io.BytesIO(zip_bytes)
    filename = result. get("filename", "result.zip")
    
    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(zip_bytes))
        }
    )

@app.post("/test-celery")
async def test_celery(request:  Request):
    """Test Celery"""
    from celery_tasks import render_download_job
    import base64
    import io
    from PIL import Image
    
    test_image = Image. new('RGB', (100, 100), color='red')
    img_buffer = io.BytesIO()
    test_image.save(img_buffer, format='PNG')
    img_buffer.seek(0)
    file_bytes_b64 = base64.b64encode(img_buffer.getvalue()).decode("utf-8")
    
    test_payload = json.dumps([{
        "area_id": "main_box",
        "text":  "Test",
        "color":  "#FFFFFF",
        "font_size": 56
    }])
    
    job = render_download_job.apply_async(
        args=[
            "template_A_portrait_1080x1350",
            test_payload,
            file_bytes_b64,
            ["en", "fr"],
            "test",
            "Test description",
            "test-user"
        ]
    )
    
    try:
        result = job.get(timeout=30)
        return JSONResponse({
            "success":  True,
            "job_id": job.id,
            "message": "✅ CELERY WORKS!"
        })
    except Exception as e:
        return JSONResponse({
            "success": False,
            "error":  str(e)
        }, status_code=500)


# Mount static files LAST (after all API routes)
app.mount("/ui", StaticFiles(directory="ui", html=True), name="ui")



if __name__ == "__main__": 
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
