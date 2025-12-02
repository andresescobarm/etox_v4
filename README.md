# Etox — Text Editor & Translator for Images

Etox is a backend + single-page UI that lets you place editable/translatable text on images (templates), preview results and download per-language PNGs bundled in a ZIP. The backend is written in FastAPI (app.py), translation logic is in tradufotos.py (uses OpenAI), and a static UI (index.html + JS) provides a simple client.

Key features
- Render multi-area templated text onto an uploaded image (letterbox/scale, alignment, background boxes).
- Highlight phrases with color and fuzzy matching across translations.
- Translate Spanish source text into multiple target languages using a multi-step OpenAI pipeline (forward, strict, refinement, triple backtranslation, QA and naturalization).
- Produce a ZIP with per-language PNGs and optional CSVs for translated captions.
- Preview endpoint returns either an image or JSON diagnostics when in debug mode.
- Concurrency: translation uses thread pool, rendering uses process pool for performance.

Table of contents
- Features
- Repository layout
- Requirements
- Configuration
- Quick start (development)
- Running with Docker (optional)
- API — endpoints and examples
- Templates, fonts & prompts
- Security notes / TODOs
- Troubleshooting & tips
- Contributing
- License

Repository layout (important files)
- app.py — FastAPI backend, rendering logic and endpoints (/render-preview, /render-download, /templates, /health)
- tradufotos.py — translation pipeline using OpenAI (translate / translate_caption)
- ui/ (mounted at `/ui`) — static web UI; main file: index.html
- templates/templates.json — templates definition file (required)
- fonts/ — font files referenced from templates (required)
- naturalization_prompts.json — prompts used by tradufotos.py (must be present next to tradufotos.py)

Requirements
- Python 3.10+ recommended
- Pip packages:
  - fastapi
  - uvicorn[standard]
  - pillow
  - python-multipart
  - openai (the official OpenAI package, matches usage in tradufotos.py)
  - typing-extensions (optional depending on Python version)

Suggested requirements.txt
```text
fastapi
uvicorn[standard]
pillow
python-multipart
openai
```

Configuration / Environment
- OpenAI API: tradufotos.py currently constructs an OpenAI client inside _get_client(). You must provide your OpenAI API credentials before using translation endpoints.
  - IMPORTANT: The repository code you provided contains a hardcoded API key inside tradufotos.py. Remove this hardcoded key immediately and use an environment variable instead.
  - Recommended change (example): read from `OPENAI_API_KEY` and create the client:
    ```py
    import os
    from openai import OpenAI

    def _get_client() -> OpenAI:
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY environment variable not set")
        return OpenAI(api_key=api_key)
    ```
  - Export the key locally:
    - macOS / Linux:
      ```bash
      export OPENAI_API_KEY="sk-..."
      ```
    - Windows (PowerShell):
      ```powershell
      $env:OPENAI_API_KEY="sk-..."
      ```

- Model & pipeline tuning:
  - `DEFAULT_MODEL` in tradufotos.py is set to `"gpt-4o"` (adjust if you need a different model or you have different access).
  - Similarity and thresholds can be configured via constants in tradufotos.py (e.g., SIMILARITY_THRESHOLD).

Quick start — development (local)
1. Create a virtual environment and install dependencies:
   ```bash
   python -m venv venv
   source venv/bin/activate       # or venv\Scripts\activate on Windows
   pip install -r requirements.txt
   ```
   Or install packages manually:
   ```bash
   pip install fastapi uvicorn pillow python-multipart openai
   ```

2. Prepare assets:
   - Place `templates/templates.json` in the project (see Templates section).
   - Place font files in `fonts/` and ensure template `font_map` references match filenames.
   - Ensure `naturalization_prompts.json` is next to `tradufotos.py`.

3. Set your OpenAI API key:
   ```bash
   export OPENAI_API_KEY="sk-..."
   ```

4. Run the app:
   ```bash
   uvicorn app:app --reload --host 0.0.0.0 --port 8000
   ```
   The static UI is mounted at `/ui`. Point your browser to http://localhost:8000/ui/index.html

Running with Docker (optional)
- You can containerize the service; a simple Dockerfile will:
  - copy files
  - install required pip packages
  - set environment variables (do NOT bake API keys into images)
  - run uvicorn

API — endpoints and examples
- GET /templates
  - Returns JSON array of templates read from `templates/templates.json`.
  - Example:
    ```bash
    curl -s http://localhost:8000/templates | jq .
    ```

- POST /render-preview
  - Form fields:
    - template_id (string)
    - payload (JSON string) — an array of text area entries (area_id, text, color, highlight_phrase, highlight_color, font_size, background_color, etc.)
    - image (file) — the image to render onto
  - Response: streaming PNG (image/png) or JSON diagnostics if `debug` is enabled in a payload entry.
  - Example (save preview to preview.png):
    ```bash
    curl -X POST "http://localhost:8000/render-preview" \
      -F "template_id=template_A" \
      -F 'payload=[{"area_id":"left_a","text":"Hola mundo","color":"#000000"}]' \
      -F "image=@/path/to/photo.png" \
      --output preview.png
    ```

- POST /render-download
  - Form fields:
    - template_id (string)
    - payload (JSON string)
    - languages (JSON array of target language codes, e.g. '["en","fr"]') — the server always includes 'es' (Spanish) first
    - base_name (string) — base file name used inside ZIP
    - image (file)
    - permanent_note (string, optional) — caption/description that will be translated and included as CSVs for non-es languages
  - Response: application/zip file (download containing PNGs and CSVs)
  - Example:
    ```bash
    curl -X POST "http://localhost:8000/render-download" \
      -F "template_id=template_A" \
      -F 'payload=[{"area_id":"left_a","text":"Hola mundo","color":"#000000"}]' \
      -F 'languages=["en","fr"]' \
      -F "base_name=my-image" \
      -F "image=@/path/to/photo.png" \
      --output my-image.zip
    ```

- GET /health
  - Lightweight health check:
    ```bash
    curl http://localhost:8000/health
    # -> {"status":"ok"}
    ```

Templates, fonts & prompts
- templates/templates.json:
  - This file defines the template canvases (width/height) and text_areas with bbox percentages, default colors, allowed colors, max/min font sizes, alignment rules, font_map, etc.
  - The backend expects templates to be present and will raise an error if templates/templates.json is missing.

- fonts/:
  - Fonts referenced by each template's `font_map` must be present in the `fonts` directory (e.g., `fonts/Inter-Regular.ttf`).

- naturalization_prompts.json:
  - Contains system prompts used by tradufotos.py for translation, strict translation, refinement, backtranslation, QA and naturalization steps.
  - Must include a top-level `languages` mapping with supported language codes.

Security notes / TODOs
- Remove the hardcoded OpenAI API key in tradufotos.py. Never commit real API keys.
- Prefer environment-driven secrets and, for deployments, use secrets managers or CI/CD secret stores.
- CORS is currently set to allow all origins. Lock it down in production.
- The UI accepts file uploads; ensure you deploy behind HTTPS and configure appropriate file size limits if exposed publicly.

Troubleshooting & tips
- If fonts are missing: app will raise 500 with "Font file missing" — ensure font files exist and template font_map keys match.
- If templates file not found: app raises RuntimeError — create templates/templates.json.
- If translation pipeline fails or is slow: check OpenAI key, quota, model access and pipeline concurrency (MAX_TRANSLATION_WORKERS / MAX_RENDER_WORKERS in app.py).
- Diagnostics: send `debug=true` in a payload entry to receive detailed layout diagnostics instead of an image (useful to tune box sizes, font sizes, and line-wrapping).

Contributing
- Please follow common contribution practices:
  - Create issues for bugs or feature requests.
  - Fork, create feature branch, and open pull requests with a clear description and small focused changes.
  - Add tests for critical rendering or parsing logic where possible.

License
- MIT License — see LICENSE file.

Maintainers / Contact
- Project: Etox — Text Editor & Translator for Images
- If you want, provide a maintainer email or GitHub user in this section.

Acknowledgements
- Uses FastAPI, Pillow (PIL) and OpenAI.
- The translation pipeline is implemented in tradufotos.py and relies on prompt templates in naturalization_prompts.json.