#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Celery tasks for background processing.
"""

import io
import json
import copy
import zipfile
from typing import List, Dict, Any
from celery_app import celery_app
from cache_manager import get_cache_manager


@celery_app.task(bind=True, name="tasks.render_download_job")
def render_download_job(
    self,
    template_id: str,
    payload: str,
    file_bytes_b64: str,  # Base64 encoded image
    languages: List[str],
    base_name: str,
    permanent_note: str,
    user_ip: str
):
    """
    Background job for rendering images with translations.
    
    Returns:
        Dict with: 
            - success: bool
            - zip_bytes_b64: Base64 encoded ZIP file (if success)
            - error: str (if failed)
    """
    import base64
    from tradufotos import translate, translate_caption
    from app import render_canvas, get_template_by_id, find_best_fuzzy_match
    
    cache = get_cache_manager()
    
    try:
        # Update task state
        self.update_state(state="PROCESSING", meta={"progress": 0, "status": "Starting..."})
        
        # Decode image
        file_bytes = base64.b64decode(file_bytes_b64)
        
        # Parse inputs
        template = get_template_by_id(template_id)
        payload_data = json.loads(payload)
        
        # Ensure Spanish is first
        all_languages = ["es"] + [lang for lang in languages if lang != "es"]
        
        self.update_state(state="PROCESSING", meta={"progress": 10, "status": "Extracting important words..."})
        
        # Extract important words (same as before)
        from app import extract_important_words_from_original, find_important_words_in_translation
        original_important_words = {}
        for idx, entry in enumerate(payload_data):
            original_text = entry. get("text", "")
            if original_text. strip():
                important_words = extract_important_words_from_original(original_text)
                if important_words:
                    original_important_words[idx] = important_words
        
        # Render images for each language
        rendered_images = []
        total_langs = len(all_languages)
        
        for lang_idx, lang in enumerate(all_languages):
            progress = 10 + int((lang_idx / total_langs) * 70)
            self.update_state(
                state="PROCESSING",
                meta={"progress": progress, "status": f"Translating to {lang}... "}
            )
            
            lang_payload = copy.deepcopy(payload_data)
            
            # Translate if not Spanish
            if lang != "es": 
                for idx, entry in enumerate(lang_payload):
                    original_text = entry.get("text", "")
                    highlight_color = entry.get("highlight_color", "")
                    base_color = entry.get("color", "#FFFFFF")
                    
                    if original_text.strip():
                        # Check cache first
                        cached = cache.get_translation(original_text, lang, "headline")
                        
                        if cached:
                            entry["text"] = cached["human"]
                        else:
                            # Translate and cache
                            result = translate(original_text, lang, user_ip)
                            entry["text"] = result. get("human", original_text)
                            cache. set_translation(original_text, lang, result, "headline")
                        
                        entry["lang"] = lang
                        entry["color"] = "#FFFFFF"
                        entry["highlight_color"] = "#FFFF02"
            
            # Render canvas
            canvas, _, _ = render_canvas(template, lang_payload, file_bytes)
            
            # Convert to PNG bytes
            img_buffer = io.BytesIO()
            canvas.save(img_buffer, format="PNG")
            img_buffer.seek(0)
            
            filename = f"{base_name}_{lang}.png"
            rendered_images.append((filename, img_buffer.getvalue()))
        
        # Generate descriptions
        self.update_state(state="PROCESSING", meta={"progress": 85, "status": "Generating descriptions..."})
        
        descriptions = {}
        if permanent_note. strip():
            for lang in all_languages:
                if lang == "es":
                    descriptions[lang] = permanent_note
                else:
                    # Check cache
                    cached = cache.get_translation(permanent_note, lang, "description")
                    if cached:
                        descriptions[lang] = cached["human"]
                    else:
                        result = translate_caption(permanent_note, lang, user_ip)
                        descriptions[lang] = result.get("human", permanent_note)
                        cache.set_translation(permanent_note, lang, result, "description")
        
        # Create ZIP
        self.update_state(state="PROCESSING", meta={"progress": 95, "status": "Creating ZIP..."})
        
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for filename, img_bytes in rendered_images:
                zf.writestr(filename, img_bytes)
            
            for lang, desc_text in descriptions.items():
                zf.writestr(f"{base_name}_{lang}_description.csv", desc_text. encode("utf-8"))
        
        zip_buffer.seek(0)
        zip_bytes_b64 = base64.b64encode(zip_buffer.getvalue()).decode("utf-8")
        
        return {
            "success": True,
            "zip_bytes_b64": zip_bytes_b64,
            "filename": f"{base_name}.zip"
        }
        
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        print(f"❌ Job failed: {error_trace}")
        
        return {
            "success": False,
            "error": str(e),
            "trace": error_trace[: 500]
        }
