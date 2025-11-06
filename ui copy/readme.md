```markdown
# Minimal Preview UI

This is a minimal static UI that posts to your running FastAPI backend `/render-preview` and displays the returned PNG.

How to use
1. Put this folder as `ui/` in your project root (same level as `backend/`), e.g. `~/Desktop/etos/ui`.
2. Make sure your FastAPI server is running:
   ```
   source venv/bin/activate
   export PYTHONPATH=.
   uvicorn backend.app:app --reload --port 8000
   ```
3. Open the UI in your browser:
   - Option A (quick, may work without a local server): open the file directly:
     - Open the file `ui/index.html` in your browser (File → Open or drag into browser).
     - If the browser blocks network requests from file:// pages, use Option B.
   - Option B (recommended): serve the ui directory and open via http:
     ```
     # from project root
     python3 -m http.server 5500
     # then open http://127.0.0.1:5500/ui/index.html
     ```
4. Use the form:
   - Select template (defaults are in the `<select>` — change as needed).
   - Type text, choose size and color, optionally upload your `sample_input.png`.
   - Click "Render" to POST to `/render-preview` and see the result in the preview pane.
   - Click "Render & Download" to fetch the rendered PNG and download it.

Notes and troubleshooting
- The form posts to `http://127.0.0.1:8000/render-preview`. Make sure your backend is accessible there and running.
- The UI uses a simple POST form (targeting an iframe) so you bypass CORS complexities. The "Render & Download" button uses `fetch()`; if your backend does not allow CORS for `fetch`, run the UI via the same origin as the backend or enable CORS in your FastAPI app.
- If the preview fails or shows a server error, check the backend terminal (uvicorn) for tracebacks — missing fonts are the most common cause of rendering errors.

Extending this UI
- Populate the template `<select>` dynamically by fetching `/templates` (requires CORS enabled).
- Add inputs for multiple text areas and map them into the `payload` array.
- Improve styling, add mobile layout, or integrate into your existing frontend.

Enjoy! If you want, I can:
- Add a small FastAPI static mount so the UI is served from the backend (one-line change in backend.app).
- Or add client-side fetching of /templates (and set up CORS handling) so the template list is dynamic.
```