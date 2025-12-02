#!/usr/bin/env python3
import csv
from tradufotos import translate, translate_caption
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import io
from PIL import Image, ImageDraw, ImageFont
import json
from pathlib import Path
from typing import List, Tuple, Optional
import re
import zipfile
import copy
from difflib import SequenceMatcher
import concurrent.futures
import multiprocessing
import time

### FUZZY MACHINE


def find_best_fuzzy_match(text, target_phrase, threshold=0.7):
    """Return the most similar substring to target_phrase in text (if above threshold)."""
    words = text.split()
    target_n = max(1, len(target_phrase.split()))
    best = None
    best_score = 0
    n = len(words)
    for i in range(n):
        for j in range(i+1, min(n+1, i+target_n+4)):  # Try chunks up to target length +3
            chunk = ' '.join(words[i:j])
            score = SequenceMatcher(None, chunk.lower(), target_phrase.lower()).ratio()
            if score > best_score:
                best_score = score
                best = chunk
    if best_score >= threshold:
        return best
    return None


app = FastAPI(title="Etos Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # change to specific origins in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/ui", StaticFiles(directory="ui", html=True), name="ui")

@app.get("/", include_in_schema=False)
def root_redirect():
    return RedirectResponse(url="/ui/index.html")


TEMPLATES_PATH = Path("templates/templates.json")
FONTS_DIR = Path("fonts")

# punctuation detection (token that is only punctuation)
_punct_re = re.compile(r'^[^\w\s]+$', flags=re.UNICODE)
def is_punctuation_token(s: str) -> bool:
    return bool(_punct_re.match(s))

# Distinguish opening vs closing punctuation so spacing rules can be correct:
OPENING_PUNCT = set(['"', '“', '«', '(', '[', '¿', '¡'])
CLOSING_PUNCT = set([',', '.', ';', ':', '!', '?', '”', '»', ')', ']', '…', '—', '–', '-'])

def contains_opening_punct(s: str) -> bool:
    """Return True if token contains any opening punctuation character."""
    return bool(s) and any(ch in OPENING_PUNCT for ch in s)

def contains_closing_punct(s: str) -> bool:
    """Return True if token contains any closing punctuation character."""
    return bool(s) and any(ch in CLOSING_PUNCT for ch in s)


def join_tokens_with_spacing(ln: List[Tuple[str, str]]) -> str:
    """
    Given a list of (token, color) pairs, join them into a string using the spacing rules:
    - No space before any token that contains closing punctuation (e.g. '.', ',', '!' etc).
    - No space after any token that contains opening punctuation (e.g. opening quote, bracket).
    - Space after a punctuation token that is a closing punctuation when it is the current token (comma behavior).
    - Otherwise, regular single space between words.
    """
    if not ln:
        return ""
    s = ""
    for idx, (w, c) in enumerate(ln):
        s += w
        if idx < len(ln) - 1:
            next_token = ln[idx + 1][0]
            # Prefer rule: if next token contains any closing punctuation, don't add a space before it.
            if contains_closing_punct(next_token):
                sep = ""
            # If current token contains opening punctuation, don't add a space after it.
            elif contains_opening_punct(w):
                sep = ""
            # If current token is a closing punctuation token, keep a space after it (comma -> space).
            elif contains_closing_punct(w):
                sep = " "
            else:
                sep = " "
            s += sep
    return s


def load_templates():
    if not TEMPLATES_PATH.exists():
        raise RuntimeError("templates/templates.json not found. Place your templates file in templates/")
    with TEMPLATES_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("templates", [])


def get_template_by_id(template_id: str):
    templates = load_templates()
    for t in templates:
        if t.get("id") == template_id:
            return t
    available = [t.get("id") for t in templates]
    raise HTTPException(status_code=404, detail={
        "error": "Template not found",
        "requested_id": template_id,
        "available_template_ids": available
    })


@app.get("/templates")
def list_templates():
    return JSONResponse({"templates": load_templates()})


def compute_bbox_pixels(template, bbox):
    W = template["size"]["width"]
    H = template["size"]["height"]
    x = int(round(bbox["x_pct"] * W))
    y = int(round(bbox["y_pct"] * H))
    w = int(round(bbox["w_pct"] * W))
    h = int(round(bbox["h_pct"] * H))
    return x, y, w, h


def wrap_text(draw: ImageDraw.Draw, text: str, font: ImageFont.FreeTypeFont, max_width: int):
    words = text.split()
    if not words:
        return [""]
    lines = []
    cur = words[0]
    for w in words[1:]:
        # don't insert a space before punctuation-only tokens
        if is_punctuation_token(w):
            test = cur + w
        else:
            test = cur + " " + w
        if draw.textlength(test, font=font) <= max_width:
            cur = test
        else:
            lines.append(cur)
            cur = w
    lines.append(cur)
    return lines


def wrap_words_with_color(draw: ImageDraw.Draw, words_with_color: List[Tuple[str, str]],
                          font: ImageFont.FreeTypeFont, max_width: int):
    """
    Wrap a list of (token, color) pairs into lines, respecting punctuation spacing rules:
    - Attach closing punctuation to previous token (no space before).
    - Do not add a space after opening punctuation.
    - Otherwise, use a single space between tokens.
    """
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

        # Build candidate text respecting opening vs closing punctuation.
        # If the next token contains any closing punctuation, attach it without a space before.
        if word_is_closing:
            cand_text = cur_text + word  # attach closing punctuation to previous token (no space before)
        else:
            # If the previous token was an opener, attach directly (no space after opener).
            if last_was_opening:
                cand_text = cur_text + word
            else:
                cand_text = cur_text + " " + word

        if draw.textlength(cand_text, font=font) <= max_width:
            cur_line.append(wcol)
            # Update cur_text and last_was_opening for next iteration
            if word_is_closing:
                cur_text = cur_text + word
                last_was_opening = False
            else:
                if last_was_opening:
                    cur_text = cur_text + word
                else:
                    cur_text = cur_text + " " + word
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
    """
    Build spans from one or multiple highlight phrases.

    Supported input for `phrase`:
      - Single phrase (exact substring): "primer vino"
      - Multiple phrases separated by '/' (user-proposed delimiter): "llamado/honor" or "llamado / honor"
        The delimiter '/' was chosen because it's uncommon in normal copy; phrases are stripped
        and matched case-insensitively.

    Matching rules:
      - Find all non-overlapping matches for the requested phrases.
      - When matches overlap, prefer the match that starts earlier; for the same start prefer the longer match.
      - Reconstruct a sequence of spans covering the whole text in order, using highlight_color for matched
        segments and base_color for everything else.

    Returns a list of {"text": ..., "color": ...} or None if phrase is falsy.
    """
    if not phrase:
        return None

    # Split by '/' delimiter and keep non-empty phrases (strip whitespace)
    raw_phrases = [p.strip() for p in phrase.split('/') if p.strip()]
    if not raw_phrases:
        return None

    flags = re.IGNORECASE | re.UNICODE

    # Collect all matches for every phrase
    matches = []
    for p in raw_phrases:
        try:
            pattern = re.compile(re.escape(p), flags=flags)
        except Exception:
            pattern = re.compile(re.escape(p), flags=flags)
        for m in pattern.finditer(text):
            matches.append((m.start(), m.end(), m.group(0)))

    if not matches:
        return [{"text": text, "color": base_color}]

    # Sort by start asc, length desc (so longer matches at same start win)
    matches.sort(key=lambda x: (x[0], -(x[1] - x[0])))

    # Build non-overlapping sequence of intervals covering matched parts only,
    # skipping matches that are fully consumed by previous selections.
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


def ellipsize_plain_last_line(draw: ImageDraw.Draw, font: ImageFont.FreeTypeFont,
                              line: str, max_width: int, ellipsis: str = "..."):
    if draw.textlength(line, font=font) <= max_width:
        return line
    words = line.split()
    if not words:
        return ellipsis if draw.textlength(ellipsis, font=font) <= max_width else ""
    while words:
        candidate = " ".join(words) + ellipsis
        if draw.textlength(candidate, font=font) <= max_width:
            return candidate
        words.pop()
    return ellipsis if draw.textlength(ellipsis, font=font) <= max_width else ""


def ellipsize_spans_last_line(draw: ImageDraw.Draw, font: ImageFont.FreeTypeFont,
                              line_words: List[Tuple[str, str]], max_width: int, ellipsis: str = "..."):
    def line_width(words_list):
        w = 0.0
        for idx, (wrd, col) in enumerate(words_list):
            if idx < len(words_list) - 1:
                next_token = words_list[idx + 1][0]
                # If the next token contains any closing punctuation, don't add space before it.
                if contains_closing_punct(next_token):
                    part = wrd
                # If current token contains opening punctuation, don't add space after it.
                elif contains_opening_punct(wrd):
                    part = wrd
                # If current token contains closing punctuation, include a space after it (comma).
                elif contains_closing_punct(wrd):
                    part = wrd + " "
                else:
                    part = wrd + " "
            else:
                part = wrd
            w += draw.textlength(part, font=font)
        return w

    if line_width(line_words) <= max_width:
        return line_words, None

    words = list(line_words)
    while words:
        w_last_color = words[-1][1] if words else None
        w_total = 0.0
        for idx, (wrd, col) in enumerate(words):
            if idx < len(words) - 1:
                next_token = words[idx + 1][0]
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
            w_total += draw.textlength(part, font=font)
        w_with_ellipsis = w_total + draw.textlength(ellipsis, font=font)
        if w_with_ellipsis <= max_width:
            return words, w_last_color
        words.pop()
    if draw.textlength(ellipsis, font=font) <= max_width:
        return [], None
    return [], None


def fit_text_in_box(draw, text: str, font_path: Path, max_font_size: int, min_font_size: int,
                    box_w: int, box_h: int, max_lines: int, line_spacing: int = 0,
                    line_spacing_mode: str = "legacy",
                    spans: List[dict] = None, default_color: str = "#000000",
                    preferred_font_size: Optional[int] = None,
                    font_size_mode: str = "auto"):
    if preferred_font_size is not None and font_size_mode == "absolute":
        font_size = int(preferred_font_size)
    else:
        if preferred_font_size is not None:
            font_size = int(preferred_font_size)
            if font_size > max_font_size:
                font_size = max_font_size
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

    # fallback to min font size
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


# Full render_canvas (unchanged from prior working implementation except punctuation spacing fixes)
def render_canvas(template, payload_data, file_bytes):
    try:
        user_img = Image.open(io.BytesIO(file_bytes)).convert("RGBA")
    except Exception:
        raise HTTPException(status_code=400, detail="Uploaded file is not a valid image")

    target_w = template["size"]["width"]
    target_h = template["size"]["height"]

    # fit user image into template canvas (preserve aspect, center with letterbox)
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

    # Growth thresholds and raster-scaling cap
    FILL_TRIGGER_RATIO = 1.00
    FILL_TARGET_RATIO = 0.90
    MAX_BITMAP_UPSCALE = 1.9

    # pre-pass to compute equalized widths for Template C/E bottom boxes
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
            used_spans = spans if spans else (build_spans_from_highlight_phrase(entry.get("text", ""), entry.get("color", template.get("allowed_colors", ["#000000"])[0]), highlight_color, highlight_phrase) if highlight_color and highlight_phrase else None)
            try:
                max_fs = area.get("max_font_size", 200)
                min_fs = area.get("min_font_size", 12)
                max_lines = area.get("max_lines", 3)
                line_spacing = int(area.get("line_spacing", 0))
                line_spacing_mode = area.get("line_spacing_mode", "legacy")
                lines, font, line_h, total_h = fit_text_in_box(
                    draw, entry.get("text", ""), font_path, max_fs, min_fs, bw, bh, max_lines,
                    line_spacing, line_spacing_mode=line_spacing_mode, spans=used_spans, default_color=entry.get("color", template.get("allowed_colors", ["#000000"])[0]),
                    preferred_font_size=entry.get("font_size", None), font_size_mode=entry.get("font_size_mode", None) or ("absolute" if entry.get("font_size") else "auto")
                )
                measured_lines_text = []
                if used_spans:
                    for ln in lines:
                        # Use join_tokens_with_spacing to build the visible string
                        measured_lines_text.append(join_tokens_with_spacing(ln))
                else:
                    measured_lines_text = list(lines)
                measured_max_w = 0
                measured_heights = []
                for ltxt in measured_lines_text:
                    bbox = draw.textbbox((0, 0), ltxt if ltxt != "" else " ", font=font)
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

    target_final_width = None
    if per_entry_final_width:
        target_final_width = max(per_entry_final_width.values())

    # Main rendering loop
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
                diagnostics.append({
                    "area_id": area_id,
                    "error": "area not found in template",
                    "template_text_areas": template.get("text_areas", [])
                })
            continue
        area = areas[0]
        bx, by, bw, bh = compute_bbox_pixels(template, area["bbox"])

        entry_bg = entry.get("background_color")
        bg = None
        try:
            if template.get("id") and re.search(r"template_(C|E)", template.get("id"), flags=re.IGNORECASE):
                bg = entry_bg if entry_bg else area.get("background_color")
            else:
                bg = area.get("background_color")
        except Exception:
            bg = area.get("background_color")

        draw_full_area_bg = False
        if bg:
            if template.get("id") and re.search(r"template_(C|E)", template.get("id"), flags=re.IGNORECASE) and entry_bg and isinstance(area.get("id",""), str) and area.get("id","").endswith("_a"):
                draw_full_area_bg = False
            else:
                draw_full_area_bg = True

        if draw_full_area_bg:
            try:
                rgba = tuple(int(bg.lstrip("#")[i:i+2], 16) for i in (0, 2, 4)) + (255,)
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
        else:
            used_spans = None

        if used_spans:
            lines, font, line_h, total_h = fit_text_in_box(
                draw, None, font_path, max_fs, min_fs, bw, bh, max_lines,
                line_spacing, line_spacing_mode=line_spacing_mode, spans=used_spans, default_color=color,
                preferred_font_size=preferred_font, font_size_mode=font_size_mode
            )
        else:
            lines, font, line_h, total_h = fit_text_in_box(
                draw, text, font_path, max_fs, min_fs, bw, bh, max_lines,
                line_spacing, line_spacing_mode=line_spacing_mode, spans=None, default_color=color,
                preferred_font_size=preferred_font, font_size_mode=font_size_mode
            )

        measured_lines_text = []
        if used_spans:
            for ln in lines:
                measured_lines_text.append(join_tokens_with_spacing(ln))
        else:
            measured_lines_text = list(lines)

        measured_line_bboxes = []
        measured_line_heights = []
        max_line_width = 0
        for ltxt in measured_lines_text:
            if ltxt == "":
                bbox = draw.textbbox((0, 0), " ", font=font)
            else:
                bbox = draw.textbbox((0, 0), ltxt, font=font)
            measured_line_bboxes.append(bbox)
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

        if font_size_mode == "absolute" and preferred_font is not None:
            if current_total_h > bh or (isinstance(lines, list) and len(lines) > max_lines):
                fs = preferred_font
                best = {
                    "font_size": fs,
                    "lines": lines,
                    "font": font,
                    "measured_heights": measured_line_heights,
                    "max_line_width": max_line_width,
                    "total_h": current_total_h
                }
                while fs + 2 <= max_fs:
                    fs += 2
                    test_font = ImageFont.truetype(str(font_path), size=fs)
                    if used_spans:
                        words_with_color = split_spans_to_words(used_spans, color)
                        test_lines = wrap_words_with_color(draw, words_with_color, test_font, bw)
                    else:
                        test_lines = wrap_text(draw, text, test_font, bw)
                    if len(test_lines) > max_lines:
                        break
                    test_heights = []
                    test_max_w = 0
                    if used_spans:
                        test_line_texts = [join_tokens_with_spacing(ln) for ln in test_lines]
                    else:
                        test_line_texts = test_lines
                    for ltxt in (test_line_texts):
                        bbox = draw.textbbox((0, 0), ltxt if ltxt != "" else " ", font=test_font)
                        test_heights.append(int(bbox[3] - bbox[1]))
                        test_w = int(bbox[2] - bbox[0])
                        if test_w > test_max_w:
                            test_max_w = test_w
                    if not test_heights:
                        continue
                    max_test_h = max(test_heights)
                    test_eff_gap = compute_effective_gap(max_test_h, line_spacing, line_spacing_mode)
                    test_total_h = max_test_h + max(0, (len(test_heights) - 1) * test_eff_gap)
                    if test_total_h >= bh * FILL_TARGET_RATIO:
                        best = {
                            "font_size": fs,
                            "lines": test_lines,
                            "font": test_font,
                            "measured_heights": test_heights,
                            "max_line_width": test_max_w,
                            "total_h": test_total_h
                        }
                        break
                    if test_total_h > best["total_h"]:
                        best = {
                            "font_size": fs,
                            "lines": test_lines,
                            "font": test_font,
                            "measured_heights": test_heights,
                            "max_line_width": test_max_w,
                            "total_h": test_total_h
                        }
                if best["total_h"] > current_total_h:
                    lines = best["lines"]
                    font = best["font"]
                    measured_line_heights = best["measured_heights"]
                    max_measured_line_h = max(measured_line_heights) if measured_line_heights else max_measured_line_h
                    max_line_width = best.get("max_line_width", max_line_width)
                    effective_gap = compute_effective_gap(max_measured_line_h, line_spacing, line_spacing_mode)
                    current_total_h = max_measured_line_h + max(0, (len(measured_line_heights) - 1) * effective_gap)
                    total_h = current_total_h
                else:
                    total_h = current_total_h
            else:
                total_h = current_total_h
        else:
            total_h = current_total_h

        if font_size_mode == "absolute":
            allowed_by_height = allowed_lines_by_height(bh, max_measured_line_h, effective_gap)
            allowed_lines = min(max_lines, allowed_by_height)
            if isinstance(lines, list) and len(lines) > allowed_lines:
                if used_spans:
                    truncated = lines[:allowed_lines]
                    last_line_words = truncated[-1]
                    new_last_words, ell_color = ellipsize_spans_last_line(draw, font, last_line_words, bw, ellipsis="...")
                    truncated[-1] = new_last_words
                    lines = truncated
                    ellipsis_info = {"append": True, "color": ell_color or (last_line_words[-1][1] if last_line_words else color)}
                else:
                    truncated = lines[:allowed_lines]
                    last_line = truncated[-1]
                    new_last_line = ellipsize_plain_last_line(draw, font, last_line, bw, ellipsis="...")
                    truncated[-1] = new_last_line
                    lines = truncated
                    ellipsis_info = {"append": False, "color": None}
            else:
                ellipsis_info = {"append": False, "color": None}
            total_h = max_measured_line_h + max(0, (len(lines) - 1) * effective_gap)
        else:
            ellipsis_info = {"append": False, "color": None}
            total_h = current_total_h

        # Render into block
        block_w = bw
        block_h = max(1, int(total_h))
        block = Image.new("RGBA", (block_w, block_h), (0, 0, 0, 0))
        bd = ImageDraw.Draw(block)

        for i, ln in enumerate(lines):
            if used_spans:
                line_w = 0.0
                for idx, (w, col) in enumerate(ln):
                    # Compute separator after current token
                    if idx < len(ln) - 1:
                        next_token = ln[idx + 1][0]
                        # If the next token contains any closing punctuation, don't add space before it.
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
                if area.get("align", "center") == "center":
                    x = (block_w - line_w) // 2
                elif area.get("align", "center") == "left":
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
                if area.get("align", "center") == "center":
                    x = (block_w - line_w) // 2
                elif area.get("align", "center") == "left":
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

            if template.get("id") and re.search(r"template_(C|E)", template.get("id"), flags=re.IGNORECASE) and entry.get("background_color") and isinstance(area.get("id",""), str) and area.get("id","").endswith("_a"):
                try:
                    box_color = entry.get("background_color")
                    rgba_box = tuple(int(box_color.lstrip("#")[i:i+2], 16) for i in (0, 2, 4)) + (255,)
                    pad_x = int(round(12 * scale_h))
                    if target_final_width:
                        this_final = drawn_w * scale_h
                        extra_total = max(0, int(round(target_final_width - this_final)))
                        extra_each_side = int(round(extra_total / 2.0))
                    else:
                        extra_each_side = 0
                    rx0 = max(0, paste_x_block - pad_x - extra_each_side)
                    rx1 = min(target_w, paste_x_block + new_w + pad_x + extra_each_side)
                    ry0 = by
                    ry1 = by + bh
                    draw.rectangle([rx0, ry0, rx1, ry1], fill=rgba_box)
                except Exception:
                    pass

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

            if template.get("id") and re.search(r"template_(C|E)", template.get("id"), flags=re.IGNORECASE) and entry.get("background_color") and isinstance(area.get("id",""), str) and area.get("id","").endswith("_a"):
                try:
                    box_color = entry.get("background_color")
                    rgba_box = tuple(int(box_color.lstrip("#")[i:i+2], 16) for i in (0, 2, 4)) + (255,)
                    pad_x = 12
                    if target_final_width:
                        this_final = drawn_w * scale_h
                        extra_total = max(0, int(round(target_final_width - this_final)))
                        extra_each_side = int(round(extra_total / 2.0))
                    else:
                        extra_each_side = 0
                    rx0 = max(0, paste_x_block - pad_x - extra_each_side)
                    rx1 = min(target_w, paste_x_block + draw_w + pad_x + extra_each_side)
                    ry0 = by
                    ry1 = by + bh
                    draw.rectangle([rx0, ry0, rx1, ry1], fill=rgba_box)
                except Exception:
                    pass

            canvas.paste(crop.convert("RGBA"), (paste_x_block, paste_y_block), crop.convert("RGBA"))

        if debug_flag:
            allowed_by_height = allowed_lines_by_height(bh, max_measured_line_h, effective_gap)
            allowed_lines = min(max_lines, allowed_by_height)
            will_truncate = isinstance(lines, list) and len(lines) > allowed_lines
            diagnostics.append({
                "template_id": template.get("id"),
                "area_id": area_id,
                "bbox_pixels": {"bx": bx, "by": by, "bw": bw, "bh": bh},
                "font_path": str(font_path),
                "requested_font_size": preferred_font,
                "font_size_mode": font_size_mode,
                "measured_line_bboxes": measured_line_bboxes,
                "measured_line_heights": measured_line_heights,
                "max_measured_line_h": max_measured_line_h,
                "effective_gap": effective_gap,
                "num_wrapped_lines": len(lines) if isinstance(lines, list) else None,
                "wrapped_lines": measured_lines_text,
                "drawn_block_bbox": bbox_non_empty,
                "drawn_block_size": [drawn_w, drawn_h],
                "final_total_h": total_h,
                "final_pasted_size": [new_w if 'new_w' in locals() else draw_w, new_h if 'new_h' in locals() else draw_h],
                "max_lines_from_template": max_lines,
                "allowed_lines_by_height": allowed_by_height,
                "allowed_lines": allowed_lines,
                "will_truncate_with_current_settings": will_truncate,
            })

    return canvas, diagnostics, any_debug


@app.post("/render-preview")
async def render_preview(template_id: str = Form(...), payload: str = Form(...), image: UploadFile = File(...)):
    try:
        payload_data = json.loads(payload)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid payload JSON")

    # Read uploaded file bytes and require non-empty image
    file_bytes = await image.read()
    if not file_bytes or len(file_bytes) == 0:
        # Return JSON error the frontend will parse and show
        return JSONResponse(status_code=400, content={"error": "Image upload is required. Please upload an image before requesting a preview."})

    try:
        template = get_template_by_id(template_id)
    except HTTPException as he:
        if isinstance(he.detail, dict):
            return JSONResponse(status_code=404, content=he.detail)
        available = [t.get("id") for t in load_templates()]
        return JSONResponse(status_code=404, content={
            "error": "Template not found",
            "requested_id": template_id,
            "available_template_ids": available
        })

    canvas, diagnostics, any_debug = render_canvas(template, payload_data, file_bytes)

    if any_debug:
        return JSONResponse({"diagnostics": diagnostics})

    out = io.BytesIO()
    canvas.convert("RGB").save(out, format="PNG", quality=90)
    out.seek(0)
    return StreamingResponse(out, media_type="image/png")


# Concurrency-enabled render_download replacement

# Tunables: adjust for your environment
MAX_TRANSLATION_WORKERS = 8        # thread workers for translate() calls
MAX_RENDER_WORKERS = max(1, multiprocessing.cpu_count() - 1)  # process workers for rendering
ZIP_COMPRESS = zipfile.ZIP_STORED  # ZIP_STORED = faster, bigger; ZIP_DEFLATED = slower, smaller

def _translate_safe(text: str, lang: str):
    """Call translate() safely and return the human result or original on failure."""
    try:
        res = translate(text, lang)
        if isinstance(res, dict) and res.get("human"):
            return res["human"]
        if isinstance(res, str):
            return res
        return text
    except Exception:
        return text

def render_for_language_worker(args):
    """
    Worker executed in a separate process.
    args: (template, per_payload, file_bytes, lang_abbr, base_name, permanent_note)
    Returns: dict with keys: lang, png_bytes (or None on error), csv_map (dict fname->bytes), error (optional)
    """
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

    # CSV for non-es and when permanent_note provided
    if lang_abbr != "es" and permanent_note and permanent_note.strip():
        try:
            tresult = translate_caption(permanent_note, lang_abbr)
            if isinstance(tresult, dict) and tresult.get("human"):
                translated_note = tresult["human"]
            elif isinstance(tresult, str):
                translated_note = tresult
            else:
                translated_note = permanent_note
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
    # Basic validation (same as original)
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
        return JSONResponse(status_code=400, content={"error": "Image upload is required. Please upload an image before downloading."})

    if not base_name:
        base_name = "download"

    try:
        template = get_template_by_id(template_id)
    except HTTPException as he:
        if isinstance(he.detail, dict):
            return JSONResponse(status_code=404, content=he.detail)
        available = [t.get("id") for t in load_templates()]
        return JSONResponse(status_code=404, content={
            "error": "Template not found",
            "requested_id": template_id,
            "available_template_ids": available
        })

    # ensure 'es' first and unique languages preserved
    langs_final = []
    if 'es' not in requested_langs:
        langs_final.append('es')
    else:
        langs_final.append('es')
    for l in requested_langs:
        if l == 'es':
            continue
        if l not in langs_final:
            langs_final.append(l)

    # 1) Collect unique source texts to translate
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

    # 2) Build translation cache for all non-es langs (use threads for network-bound translate)
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

    # 3) Build per-language payloads using cached translations and fuzzy matching for highlight_phrase
    per_language_payloads = {}
    for lang_abbr in langs_final:
        per_payload = []
        for entry in payload_data:
            ecopy = copy.deepcopy(entry)
            ecopy['lang'] = lang_abbr

            if lang_abbr != "es":
                txt = ecopy.get("text", "")
                if txt and txt.strip():
                    ecopy["text"] = translation_cache.get((txt, lang_abbr), txt)

            if lang_abbr != "es" and "highlight_phrase" in ecopy and ecopy["highlight_phrase"]:
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

    # 4) Prepare worker args and render in parallel (ProcessPoolExecutor)
    worker_args = []
    for lang_abbr in langs_final:
        worker_args.append((template, per_language_payloads[lang_abbr], file_bytes, lang_abbr, base_name, permanent_note))

    results = []
    # Use processes for CPU-bound rendering
    with concurrent.futures.ProcessPoolExecutor(max_workers=MAX_RENDER_WORKERS) as pe:
        futures = {pe.submit(render_for_language_worker, arg): arg[3] for arg in worker_args}
        for fut in concurrent.futures.as_completed(futures):
            lang = futures[fut]
            try:
                res = fut.result()
                results.append(res)
            except Exception as e:
                results.append({"lang": lang, "png_bytes": None, "csv_map": {}, "error": repr(e)})

    # 5) Build zip in main process
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, mode="w", compression=ZIP_COMPRESS) as zf:
        for r in results:
            lang = r.get("lang")
            if r.get("png_bytes"):
                zf.writestr(f"{base_name}_{lang}.png", r["png_bytes"])
            else:
                zf.writestr(f"{base_name}_{lang}_ERROR.txt", f"Failed to render {lang}: {r.get('error','unknown')}".encode("utf-8"))
            for fname, content in r.get("csv_map", {}).items():
                zf.writestr(fname, content)

    zip_buffer.seek(0)
    headers = {"Content-Disposition": f'attachment; filename=\"{base_name}.zip\"'}
    return StreamingResponse(zip_buffer, media_type="application/zip", headers=headers)


@app.get("/health")
def health_check():
    return {"status": "ok"}


if __name__ == "__main__":
    import os
    import uvicorn

    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("backend.app:app", host="0.0.0.0", port=port)