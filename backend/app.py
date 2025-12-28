#!/usr/bin/env python3
import csv
import io
import json
import re
import zipfile
import copy
import concurrent.futures
import multiprocessing
from difflib import SequenceMatcher
from pathlib import Path
from typing import List, Tuple, Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageDraw, ImageFont, features

from tradufotos import translate, translate_caption

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
    print("⚠️  WARNING: libraqm not available.  Complex scripts may not render correctly.")
    print("   Install:  brew install libraqm fribidi harfbuzz && pip install --upgrade Pillow --no-cache-dir")
else:
    print("✅ libraqm available - complex text shaping enabled")

# ============================================================
# RTL AND COMPLEX SCRIPT DETECTION
# ============================================================

RTL_LANGUAGES = {"ar", "he", "fa", "ur"}
COMPLEX_SCRIPT_LANGUAGES = {"ar", "he", "hi", "bn", "ta", "te", "th", "ml", "kn", "gu", "pa", "my", "km", "lo"}


def is_rtl_language(lang: str) -> bool:
    return lang in RTL_LANGUAGES


def needs_complex_shaping(lang: str) -> bool:
    return lang in COMPLEX_SCRIPT_LANGUAGES

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
            score = SequenceMatcher(None, chunk.lower(), target_phrase.lower()).ratio()
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/ui", StaticFiles(directory="ui", html=True), name="ui")


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


def join_tokens_with_spacing(ln: List[Tuple[str, str]]) -> str:
    if not ln:
        return ""
    s = ""
    for idx, (w, c) in enumerate(ln):
        s += w
        if idx < len(ln) - 1:
            next_token = ln[idx + 1][0]
            if contains_closing_punct(next_token):
                sep = ""
            elif contains_opening_punct(w):
                sep = ""
            elif contains_closing_punct(w):
                sep = " "
            else:
                sep = " "
            s += sep
    return s

# ============================================================
# TEMPLATE LOADING
# ============================================================


def load_templates():
    if not TEMPLATES_PATH.exists():
        raise RuntimeError("templates/templates.json not found.")
    with TEMPLATES_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("templates", [])


def get_template_by_id(template_id: str):
    templates = load_templates()
    for t in templates:
        if t.get("id") == template_id:
            return t
    available = [t.get("id") for t in templates]
    raise HTTPException(
        status_code=404,
        detail={
            "error": "Template not found",
            "requested_id": template_id,
            "available_template_ids": available,
        },
    )


@app.get("/templates")
def list_templates():
    return JSONResponse({"templates": load_templates()})

# ============================================================
# TEXT RENDERING WITH LIBRAQM SUPPORT
# ============================================================


def get_text_length_shaped(draw: ImageDraw.Draw, text: str, font: ImageFont.FreeTypeFont) -> float:
    return draw.textlength(text, font=font)


def get_text_bbox_shaped(
    draw: ImageDraw.Draw, position: Tuple[int, int], text: str, font: ImageFont.FreeTypeFont
) -> Tuple[int, int, int, int]:
    return draw.textbbox(position, text, font=font)

# ============================================================
# TEXT WRAPPING
# ============================================================


def wrap_text(draw: ImageDraw.Draw, text: str, font: ImageFont.FreeTypeFont, max_width: int):
    words = text.split()
    if not words:
        return [""]
    lines = []
    cur = words[0]
    for w in words[1:]:
        if is_punctuation_token(w):
            test = cur + w
        else:
            test = cur + " " + w
        if get_text_length_shaped(draw, test, font) <= max_width:
            cur = test
        else:
            lines.append(cur)
            cur = w
    lines.append(cur)
    return lines


def wrap_words_with_color(
    draw: ImageDraw.Draw, words_with_color: List[Tuple[str, str]], font: ImageFont.FreeTypeFont, max_width: int
):
    if not words_with_color:
        return [[]]
    lines = []
    cur_line = [words_with_color[0]]
    cur_text = words_with_color[0][0]
    last_was_opening = contains_opening_punct(cur_text)
    for wcol in words_with_color[1:]:
        word = wcol[0]
        word_is_opening = contains_opening_punct(word)
        word_is_closing = contains_closing_punct(word)
        if word_is_closing:
            cand_text = cur_text + word
        else:
            cand_text = cur_text + (word if last_was_opening else " " + word)
        if get_text_length_shaped(draw, cand_text, font) <= max_width:
            cur_line.append(wcol)
            if word_is_closing:
                cur_text = cur_text + word
                last_was_opening = False
            else:
                cur_text = cur_text + (word if last_was_opening else " " + word)
                last_was_opening = word_is_opening
        else:
            lines.append(cur_line)
            cur_line = [wcol]
            cur_text = word
            last_was_opening = word_is_opening
    lines.append(cur_line)
    return lines


def split_spans_to_words(spans: List[dict], default_color: str):
    words = []
    token_re = re.compile(r"\w+|[^\w\s]+", flags=re.UNICODE)
    for span in spans:
        text = span.get("text", "")
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
        return [{"text": text, "color": base_color}]
    matches.sort(key=lambda x: (x[0], -(x[1] - x[0])))
    merged = []
    cur_idx = 0
    for start, end, mtext in matches:
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
    spans: List[dict] = []
    for s, e, is_high in merged:
        seg = text[s:e]
        if not seg:
            continue
        spans.append({"text": seg, "color": highlight_color if is_high else base_color})
    if not spans:
        return [{"text": text, "color": base_color}]
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


def allowed_lines_by_height(box_h: int, line_h: int, effective_gap: int):
    if box_h < line_h:
        return 1
    if effective_gap <= 0:
        return 1
    return 1 + int((box_h - line_h) // effective_gap)


def ellipsize_plain_last_line(draw: ImageDraw.Draw, font: ImageFont.FreeTypeFont, line: str, max_width: int, ellipsis: str = "..."):
    if get_text_length_shaped(draw, line, font) <= max_width:
        return line
    words = line.split()
    if not words:
        return ellipsis if get_text_length_shaped(draw, ellipsis, font) <= max_width else ""
    while words:
        candidate = " ".join(words) + ellipsis
        if get_text_length_shaped(draw, candidate, font) <= max_width:
            return candidate
        words.pop()
    return ellipsis if get_text_length_shaped(draw, ellipsis, font) <= max_width else ""


def ellipsize_spans_last_line(draw: ImageDraw.Draw, font: ImageFont.FreeTypeFont, line_words: List[Tuple[str, str]], max_width: int, ellipsis: str = "..."):
    def line_width(words_list):
        w = 0.0
        for idx, (wrd, col) in enumerate(words_list):
            if idx < len(words_list) - 1:
                next_token = words_list[idx + 1][0]
                if contains_closing_punct(next_token):
                    part = wrd
                elif contains_opening_punct(wrd):
                    part = wrd
                elif contains_closing_punct(wrd):
                    part = wrd + " "
                else:
                    part = wrd + " "
            else:
                part = wrd
            w += get_text_length_shaped(draw, part, font)
        return w

    if line_width(line_words) <= max_width:
        return line_words, None

    words = list(line_words)
    while words:
        w_total = line_width(words)
        w_with_ellipsis = w_total + get_text_length_shaped(draw, ellipsis, font)
        if w_with_ellipsis <= max_width:
            return words, words[-1][1] if words else None
        words.pop()
    if get_text_length_shaped(draw, ellipsis, font) <= max_width:
        return [], None
    return [], None

# ============================================================
# MAIN CANVAS RENDERING
# ============================================================


def compute_bbox_pixels(template, bbox):
    W = template["size"]["width"]
    H = template["size"]["height"]
    x = int(round(bbox["x_pct"] * W))
    y = int(round(bbox["y_pct"] * H))
    w = int(round(bbox["w_pct"] * W))
    h = int(round(bbox["h_pct"] * H))
    return x, y, w, h


def fit_text_in_box(
    draw,
    text: str,
    font_path: Path,
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
):
    if preferred_font_size is not None and font_size_mode == "absolute":
        font_size = int(preferred_font_size)
    else:
        if preferred_font_size is not None:
            font_size = min(int(preferred_font_size), max_font_size)
        else:
            font_size = max_font_size

    font = ImageFont.truetype(str(font_path), size=font_size)

    if font_size_mode == "absolute" and preferred_font_size is not None:
        if spans:
            words_with_color = split_spans_to_words(spans, default_color)
            lines = wrap_words_with_color(draw, words_with_color, font, box_w)
        else:
            lines = wrap_text(draw, text, font, box_w)
        ascent, descent = font.getmetrics()
        line_h = ascent + descent
        effective_gap = compute_effective_gap(line_h, line_spacing, line_spacing_mode)
        total_h = line_h + max(0, (len(lines) - 1) * effective_gap)
        return lines, font, line_h, total_h

    while font_size >= min_font_size:
        font = ImageFont.truetype(str(font_path), size=font_size)
        if spans:
            words_with_color = split_spans_to_words(spans, default_color)
            lines = wrap_words_with_color(draw, words_with_color, font, box_w)
            num_lines = len(lines)
        else:
            lines = wrap_text(draw, text, font, box_w)
            num_lines = len(lines)
        if num_lines <= max_lines:
            ascent, descent = font.getmetrics()
            line_h = ascent + descent
            effective_gap = compute_effective_gap(line_h, line_spacing, line_spacing_mode)
            total_h = line_h + max(0, (num_lines - 1) * effective_gap)
            if total_h <= box_h:
                return lines, font, line_h, total_h
        font_size -= 2
        if font_size < min_font_size:
            break

    font = ImageFont.truetype(str(font_path), size=min_font_size)
    if spans:
        words_with_color = split_spans_to_words(spans, default_color)
        lines = wrap_words_with_color(draw, words_with_color, font, box_w)
        ascent, descent = font.getmetrics()
        line_h = ascent + descent
        effective_gap = compute_effective_gap(line_h, line_spacing, line_spacing_mode)
        total_h = line_h + max(0, (len(lines) - 1) * effective_gap)
        return lines[:max_lines], font, line_h, total_h
    else:
        lines = wrap_text(draw, text, font, box_w)
        ascent, descent = font.getmetrics()
        line_h = ascent + descent
        effective_gap = compute_effective_gap(line_h, line_spacing, line_spacing_mode)
        total_h = line_h + max(0, (len(lines) - 1) * effective_gap)
        return lines[:max_lines], font, line_h, total_h

# Helper: sanitize translation outputs


def _clean_translation(result, original_text):
    """Ensure we don't propagate LLM error strings as translations."""
    if isinstance(result, dict):
        if result.get("human") and not str(result["human"]).startswith("Error:"):
            return result["human"]
        return original_text
    if isinstance(result, str):
        if result.startswith("Error:"):
            return original_text
        return result
    return original_text


# ============================================================
# MAIN CANVAS RENDERING
# ============================================================


def render_canvas(template, payload_data, file_bytes):
    try:
        user_img = Image.open(io.BytesIO(file_bytes)).convert("RGBA")
    except Exception:
        raise HTTPException(status_code=400, detail="Uploaded file is not a valid image")

    target_w = template["size"]["width"]
    target_h = template["size"]["height"]

    src_w, src_h = user_img.size
    src_ratio = src_w / src_h if src_h != 0 else 1.0
    target_ratio = target_w / target_h if target_h != 0 else 1.0
    if src_ratio > target_ratio:
        new_w = target_w
        new_h = int(round(target_w / src_ratio))
    else:
        new_h = target_h
        new_w = int(round(target_h * src_ratio))
    user_resized = user_img.resize((new_w, new_h), Image.LANCZOS)
    canvas = Image.new("RGBA", (target_w, target_h), (255, 255, 255, 255))
    paste_x = (target_w - new_w) // 2
    paste_y = (target_h - new_h) // 2
    canvas.paste(user_resized, (paste_x, paste_y))

    draw = ImageDraw.Draw(canvas)
    diagnostics = []
    any_debug = False
    FILL_TARGET_RATIO = 0.90
    MAX_BITMAP_UPSCALE = 1.9

    per_entry_final_width = {}
    if template.get("id") and re.search(r"template_(C|E)", template.get("id"), flags=re.IGNORECASE):
        for idx, entry in enumerate(payload_data):
            area_id = entry.get("area_id")
            if not isinstance(area_id, str) or not area_id.endswith("_a"):
                continue
            if not entry.get("background_color"):
                continue
            areas = [a for a in template.get("text_areas", []) if a["id"] == area_id]
            if not areas:
                continue
            area = areas[0]
            bx, by, bw, bh = compute_bbox_pixels(template, area["bbox"])
            lang = entry.get("lang", "default")
            font_key = lang if lang in template.get("font_map", {}) else "default"
            font_filename = template.get("font_map", {}).get(font_key, template.get("font_map", {}).get("default"))
            font_path = FONTS_DIR / font_filename
            if not font_path.exists():
                continue
            spans = entry.get("spans", None)
            highlight_color = entry.get("highlight_color", None)
            highlight_phrase = entry.get("highlight_phrase", None)
            used_spans = spans if spans else (
                build_spans_from_highlight_phrase(
                    entry.get("text", ""),
                    entry.get("color", template.get("allowed_colors", ["#000000"])[0]),
                    highlight_color,
                    highlight_phrase,
                )
                if highlight_color and highlight_phrase
                else None
            )
            try:
                max_fs = area.get("max_font_size", 200)
                min_fs = area.get("min_font_size", 12)
                area_max_lines = area.get("max_lines", 3)
                line_spacing = int(area.get("line_spacing", 0))
                line_spacing_mode = area.get("line_spacing_mode", "legacy")
                lines, font, line_h, total_h = fit_text_in_box(
                    draw,
                    entry.get("text", ""),
                    font_path,
                    max_fs,
                    min_fs,
                    bw,
                    bh,
                    area_max_lines,
                    line_spacing,
                    line_spacing_mode=line_spacing_mode,
                    spans=used_spans,
                    default_color=entry.get("color", template.get("allowed_colors", ["#000000"])[0]),
                    preferred_font_size=entry.get("font_size", None),
                    font_size_mode=entry.get("font_size_mode", None) or ("absolute" if entry.get("font_size") else "auto"),
                )
                measured_lines_text = []
                if used_spans:
                    for ln in lines:
                        measured_lines_text.append(join_tokens_with_spacing(ln))
                else:
                    measured_lines_text = list(lines)
                measured_max_w = 0
                measured_heights = []
                for ltxt in measured_lines_text:
                    bbox = get_text_bbox_shaped(draw, (0, 0), ltxt if ltxt != "" else " ", font)
                    w = int(bbox[2] - bbox[0])
                    h = int(bbox[3] - bbox[1])
                    measured_heights.append(h)
                    if w > measured_max_w:
                        measured_max_w = w
                if measured_heights:
                    max_measured_line_h = max(measured_heights)
                else:
                    ascent, descent = font.getmetrics()
                    max_measured_line_h = ascent + descent
                effective_gap = compute_effective_gap(max_measured_line_h, line_spacing, line_spacing_mode)
                current_total_h = max_measured_line_h + max(0, (len(measured_heights) - 1) * effective_gap)
                drawn_w = measured_max_w
                drawn_h = int(current_total_h) if current_total_h > 0 else 1
                try:
                    current_font_size = font.size
                except Exception:
                    current_font_size = entry.get("font_size", None) or max_fs
                max_scale_by_font = max_fs / max(1, current_font_size)
                max_scale = min(MAX_BITMAP_UPSCALE, max_scale_by_font)
                scale_vert = (bh / max(1, drawn_h)) if drawn_h > 0 else 1.0
                scale_h = min(scale_vert, max_scale)
                scale_h = min(scale_h, (bw / max(1, drawn_w))) if drawn_w > 0 else scale_h
                if entry.get("font_size", None) is not None:
                    scale_h = min(scale_h, 1.0)
                final_width = drawn_w * scale_h
                per_entry_final_width[idx] = final_width
            except Exception:
                continue

    target_final_width = max(per_entry_final_width.values()) if per_entry_final_width else None

    for entry_idx, entry in enumerate(payload_data):
        area_id = entry.get("area_id")
        text = entry.get("text", "")
        spans = entry.get("spans", None)
        lang = entry.get("lang", "default")
        color = entry.get("color", template.get("allowed_colors", ["#000000"])[0])
        highlight_color = entry.get("highlight_color", None)
        highlight_phrase = entry.get("highlight_phrase", None)
        font_size_hint_raw = entry.get("font_size", None)
        preferred_font: Optional[int] = None
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

        dx_pct = float(entry.get("dx_pct", 0.0))
        dy_pct = float(entry.get("dy_pct", 0.0))
        debug_flag = bool(entry.get("debug", False))
        if debug_flag:
            any_debug = True

        areas = [a for a in template.get("text_areas", []) if a["id"] == area_id]
        if not areas:
            if debug_flag:
                diagnostics.append({"area_id": area_id, "error": "area not found"})
            continue
        area = areas[0]
        bx, by, bw, bh = compute_bbox_pixels(template, area["bbox"])

        entry_bg = entry.get("background_color")
        if entry_bg:
            try:
                rgba = tuple(int(entry_bg.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)) + (255,)
                draw.rectangle([bx, by, bx + bw, by + bh], fill=rgba)
            except Exception:
                pass

        font_key = lang if lang in template.get("font_map", {}) else "default"
        font_filename = template.get("font_map", {}).get(font_key, template.get("font_map", {}).get("default"))
        font_path = FONTS_DIR / font_filename
        if not font_path.exists():
            raise HTTPException(status_code=500, detail=f"Font file missing: {font_path}")

        max_fs = area.get("max_font_size", 200)
        min_fs = area.get("min_font_size", 12)
        max_lines = area.get("max_lines", 3)
        line_spacing = int(area.get("line_spacing", 0))
        line_spacing_mode = area.get("line_spacing_mode", "legacy")

        used_spans = None
        if spans:
            used_spans = spans
        elif highlight_color and highlight_phrase:
            used_spans = build_spans_from_highlight_phrase(text, color, highlight_color, highlight_phrase)

        if used_spans:
            lines, font, line_h, total_h = fit_text_in_box(
                draw,
                None,
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
            )
        else:
            lines, font, line_h, total_h = fit_text_in_box(
                draw,
                text,
                font_path,
                max_fs,
                min_fs,
                bw,
                bh,
                max_lines,
                line_spacing,
                line_spacing_mode=line_spacing_mode,
                spans=None,
                default_color=color,
                preferred_font_size=preferred_font,
                font_size_mode=font_size_mode,
            )

        measured_lines_text = []
        if used_spans:
            for ln in lines:
                measured_lines_text.append(join_tokens_with_spacing(ln))
        else:
            measured_lines_text = list(lines)

        measured_line_heights = []
        max_line_width = 0
        for ltxt in measured_lines_text:
            bbox = get_text_bbox_shaped(draw, (0, 0), ltxt if ltxt != "" else " ", font)
            h = int(bbox[3] - bbox[1])
            measured_line_heights.append(h)
            w = int(bbox[2] - bbox[0])
            if w > max_line_width:
                max_line_width = w

        if measured_line_heights:
            max_measured_line_h = max(measured_line_heights)
        else:
            ascent, descent = font.getmetrics()
            max_measured_line_h = ascent + descent

        effective_gap = compute_effective_gap(max_measured_line_h, line_spacing, line_spacing_mode)
        current_total_h = max_measured_line_h + max(0, (len(measured_line_heights) - 1) * effective_gap)
        ellipsis_info = {"append": False, "color": None}

        if font_size_mode == "absolute":
            allowed_by_height = allowed_lines_by_height(bh, max_measured_line_h, effective_gap)
            allowed_lines_count = min(max_lines, allowed_by_height)
            if isinstance(lines, list) and len(lines) > allowed_lines_count:
                if used_spans:
                    truncated = lines[:allowed_lines_count]
                    last_line_words = truncated[-1]
                    new_last_words, ell_color = ellipsize_spans_last_line(draw, font, last_line_words, bw, ellipsis="...")
                    truncated[-1] = new_last_words
                    lines = truncated
                    ellipsis_info = {"append": True, "color": ell_color or (last_line_words[-1][1] if last_line_words else color)}
                else:
                    truncated = lines[:allowed_lines_count]
                    last_line = truncated[-1]
                    new_last_line = ellipsize_plain_last_line(draw, font, last_line, bw, ellipsis="...")
                    truncated[-1] = new_last_line
                    lines = truncated
            total_h = max_measured_line_h + max(0, (len(lines) - 1) * effective_gap)
        else:
            total_h = current_total_h

        block_w = bw
        block_h = max(1, int(total_h))
        block = Image.new("RGBA", (block_w, block_h), (0, 0, 0, 0))
        bd = ImageDraw.Draw(block)

        for i, ln in enumerate(lines):
            if used_spans:
                line_w = 0.0
                for idx, (w, col) in enumerate(ln):
                    if idx < len(ln) - 1:
                        next_token = ln[idx + 1][0]
                        if contains_closing_punct(next_token):
                            sep = ""
                        elif contains_opening_punct(w):
                            sep = ""
                        elif contains_closing_punct(w):
                            sep = " "
                        else:
                            sep = " "
                    else:
                        sep = ""
                    part = w + sep
                    line_w += bd.textlength(part, font=font)
                align = area.get("align", "center")
                if align == "center":
                    x = (block_w - line_w) // 2
                elif align == "left":
                    x = 0
                else:
                    x = block_w - line_w
                line_text = join_tokens_with_spacing(ln) if ln else " "
                bbox = bd.textbbox((0, 0), line_text if line_text != "" else " ", font=font)
                y_top = int(i * effective_gap - bbox[1])
                cur_x = x
                for idx, (w, col) in enumerate(ln):
                    if idx < len(ln) - 1:
                        next_token = ln[idx + 1][0]
                        if contains_closing_punct(next_token):
                            sep = ""
                        elif contains_opening_punct(w):
                            sep = ""
                        elif contains_closing_punct(w):
                            sep = " "
                        else:
                            sep = " "
                    else:
                        sep = ""
                    draw_text = w + sep
                    bd.text((cur_x, y_top), draw_text, font=font, fill=col)
                    cur_x += bd.textlength(draw_text, font=font)
                if i == len(lines) - 1 and ellipsis_info.get("append", False):
                    ell_color = ellipsis_info.get("color") or color
                    bd.text((cur_x, y_top), "...", font=font, fill=ell_color)
            else:
                line = ln
                line_w = bd.textlength(line, font=font)
                align = area.get("align", "center")
                if align == "center":
                    x = (block_w - line_w) // 2
                elif align == "left":
                    x = 0
                else:
                    x = block_w - line_w
                bbox = bd.textbbox((0, 0), line if line != "" else " ", font=font)
                y_top = int(i * effective_gap - bbox[1])
                bd.text((x, y_top), line, font=font, fill=color)

        bbox_non_empty = block.getbbox()
        if bbox_non_empty:
            drawn_w = bbox_non_empty[2] - bbox_non_empty[0]
            drawn_h = bbox_non_empty[3] - bbox_non_empty[1]
        else:
            drawn_w = block_w
            drawn_h = block_h

        try:
            current_font_size = font.size
        except Exception:
            current_font_size = preferred_font or max_fs

        max_scale_by_font = max_fs / max(1, current_font_size)
        MAX_BITMAP_UPSCALE = 1.9
        max_scale = min(MAX_BITMAP_UPSCALE, max_scale_by_font)
        scale_vert = (bh / max(1, drawn_h)) if drawn_h > 0 else 1.0
        scale_h = min(scale_vert, max_scale)
        scale_h = min(scale_h, (bw / max(1, drawn_w)))
        if preferred_font is not None:
            scale_h = min(scale_h, 1.0)

        v_align = area.get("v_align", "middle")

        if scale_h > 1.01:
            new_w = int(round(drawn_w * scale_h))
            new_h = int(round(drawn_h * scale_h))
            if bbox_non_empty:
                crop = block.crop(bbox_non_empty)
            else:
                crop = block
            scaled = crop.resize((new_w, new_h), resample=Image.LANCZOS)
            paste_x_block = bx + (bw - new_w) // 2
            if v_align == "top":
                paste_y_block = by
            elif v_align == "bottom":
                paste_y_block = by + bh - new_h
            else:
                paste_y_block = by + (bh - new_h) // 2
            paste_x_block = paste_x_block + int(round(dx_pct * bw))
            paste_y_block = paste_y_block + int(round(dy_pct * bh))

            canvas.paste(scaled.convert("RGBA"), (paste_x_block, paste_y_block), scaled.convert("RGBA"))
        else:
            if bbox_non_empty:
                crop = block.crop(bbox_non_empty)
                draw_w = bbox_non_empty[2] - bbox_non_empty[0]
                draw_h = bbox_non_empty[3] - bbox_non_empty[1]
            else:
                crop = block
                draw_w = block_w
                draw_h = block_h
            paste_x_block = bx + (bw - draw_w) // 2
            if v_align == "top":
                paste_y_block = by
            elif v_align == "bottom":
                paste_y_block = by + bh - draw_h
            else:
                paste_y_block = by + (bh - draw_h) // 2
            paste_x_block = paste_x_block + int(round(dx_pct * bw))
            paste_y_block = paste_y_block + int(round(dy_pct * bh))

            canvas.paste(crop.convert("RGBA"), (paste_x_block, paste_y_block), crop.convert("RGBA"))

    return canvas, diagnostics, any_debug

# ============================================================
# API ENDPOINTS
# ============================================================


@app.post("/render-preview")
async def render_preview(template_id: str = Form(...), payload: str = Form(...), image: UploadFile = File(...)):
    try:
        payload_data = json.loads(payload)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid payload JSON")
    file_bytes = await image.read()
    if not file_bytes or len(file_bytes) == 0:
        return JSONResponse(status_code=400, content={"error": "Image upload is required."})
    try:
        template = get_template_by_id(template_id)
    except HTTPException as he:
        if isinstance(he.detail, dict):
            return JSONResponse(status_code=404, content=he.detail)
        available = [t.get("id") for t in load_templates()]
        return JSONResponse(status_code=404, content={"error": "Template not found", "requested_id": template_id, "available_template_ids": available})
    canvas, diagnostics, any_debug = render_canvas(template, payload_data, file_bytes)
    if any_debug:
        return JSONResponse({"diagnostics": diagnostics})
    out = io.BytesIO()
    canvas.convert("RGB").save(out, format="PNG", quality=90)
    out.seek(0)
    return StreamingResponse(out, media_type="image/png")

# ============================================================
# DOWNLOAD ENDPOINT
# ============================================================

MAX_TRANSLATION_WORKERS = 8
MAX_RENDER_WORKERS = max(1, multiprocessing.cpu_count() - 1)
ZIP_COMPRESS = zipfile.ZIP_STORED


def _translate_safe(text: str, lang: str):
    try:
        res = translate(text, lang)
        return _clean_translation(res, text)
    except Exception:
        return text


def render_for_language_worker(args):
    template, per_payload, file_bytes, lang_abbr, base_name, permanent_note = args
    result = {"lang": lang_abbr, "png_bytes": None, "csv_map": {}, "error": None}
    try:
        canvas, diagnostics, any_debug = render_canvas(template, per_payload, file_bytes)
        img_buf = io.BytesIO()
        canvas.convert("RGB").save(img_buf, format="PNG", quality=90)
        img_buf.seek(0)
        result["png_bytes"] = img_buf.read()
    except Exception as e:
        result["error"] = f"render error: {repr(e)}"
        return result
    if lang_abbr != "es" and permanent_note and permanent_note.strip():
        try:
            tresult = translate_caption(permanent_note, lang_abbr)
            translated_note = _clean_translation(tresult, permanent_note)
        except Exception:
            translated_note = permanent_note
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(["es", lang_abbr])
        writer.writerow([permanent_note, translated_note])
        csv_bytes = out.getvalue().encode("utf-8")
        out.close()
        result["csv_map"][f"{base_name}_{lang_abbr}.csv"] = csv_bytes
    return result


@app.post("/render-download")
async def render_download(
    template_id: str = Form(...),
    payload: str = Form(...),
    languages: str = Form(...),
    base_name: str = Form(...),
    image: UploadFile = File(...),
    permanent_note: str = Form(""),
):
    try:
        payload_data = json.loads(payload)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid payload JSON")
    try:
        requested_langs = json.loads(languages)
        if not isinstance(requested_langs, list):
            raise ValueError()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid languages JSON array")
    file_bytes = await image.read()
    if not file_bytes or len(file_bytes) == 0:
        return JSONResponse(status_code=400, content={"error": "Image upload is required."})
    if not base_name:
        base_name = "download"
    try:
        template = get_template_by_id(template_id)
    except HTTPException as he:
        if isinstance(he.detail, dict):
            return JSONResponse(status_code=404, content=he.detail)
        available = [t.get("id") for t in load_templates()]
        return JSONResponse(status_code=404, content={"error": "Template not found", "requested_id": template_id, "available_template_ids": available})

    langs_final = []
    if "es" not in requested_langs:
        langs_final.append("es")
    else:
        langs_final.append("es")
    for l in requested_langs:
        if l == "es":
            continue
        if l not in langs_final:
            langs_final.append(l)

    texts_to_translate = set()
    for entry in payload_data:
        txt = entry.get("text", "")
        if txt and txt.strip():
            texts_to_translate.add(txt)
        hp = entry.get("highlight_phrase")
        if hp and hp.strip():
            texts_to_translate.add(hp)
    if permanent_note and permanent_note.strip():
        texts_to_translate.add(permanent_note)

    translation_cache = {}
    for lang_abbr in langs_final:
        if lang_abbr == "es":
            for t in texts_to_translate:
                translation_cache[(t, "es")] = t
            continue
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_TRANSLATION_WORKERS) as tex:
            futures = {tex.submit(_translate_safe, t, lang_abbr): t for t in texts_to_translate}
            for fut in concurrent.futures.as_completed(futures):
                src = futures[fut]
                try:
                    tr = fut.result()
                except Exception:
                    tr = src
                translation_cache[(src, lang_abbr)] = tr

    per_language_payloads = {}
    for lang_abbr in langs_final:
        per_payload = []
        for entry in payload_data:
            ecopy = copy.deepcopy(entry)
            ecopy["lang"] = lang_abbr
            if lang_abbr != "es":
                txt = ecopy.get("text", "")
                if txt and txt.strip():
                    ecopy["text"] = translation_cache.get((txt, lang_abbr), txt)
            if lang_abbr != "es" and ecopy.get("highlight_phrase"):
                hp = ecopy["highlight_phrase"]
                t_phrase = translation_cache.get((hp, lang_abbr), hp)
                if ecopy.get("text"):
                    match = find_best_fuzzy_match(ecopy["text"], t_phrase, threshold=0.7)
                    if match:
                        ecopy["highlight_phrase"] = match
                    else:
                        ecopy["highlight_phrase"] = ""
                else:
                    ecopy["highlight_phrase"] = ""
            per_payload.append(ecopy)
        per_language_payloads[lang_abbr] = per_payload

    worker_args = []
    for lang_abbr in langs_final:
        worker_args.append((template, per_language_payloads[lang_abbr], file_bytes, lang_abbr, base_name, permanent_note))

    results = []
    with concurrent.futures.ProcessPoolExecutor(max_workers=MAX_RENDER_WORKERS) as pe:
        futures = {pe.submit(render_for_language_worker, arg): arg[3] for arg in worker_args}
        for fut in concurrent.futures.as_completed(futures):
            lang = futures[fut]
            try:
                res = fut.result()
                results.append(res)
            except Exception as e:
                results.append({"lang": lang, "png_bytes": None, "csv_map": {}, "error": repr(e)})

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, mode="w", compression=ZIP_COMPRESS) as zf:
        for r in results:
            lang = r.get("lang")
            if r.get("png_bytes"):
                zf.writestr(f"{base_name}_{lang}.png", r["png_bytes"])
            else:
                zf.writestr(
                    f"{base_name}_{lang}_ERROR.txt",
                    f"Failed to render {lang}: {r.get('error', 'unknown')}".encode("utf-8"),
                )
            for fname, content in r.get("csv_map", {}).items():
                zf.writestr(fname, content)

    zip_buffer.seek(0)
    headers = {"Content-Disposition": f'attachment; filename="{base_name}.zip"'}
    return StreamingResponse(zip_buffer, media_type="application/zip", headers=headers)


@app.get("/health")
def health_check():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("app:app", host="0.0.0.0", port=port)