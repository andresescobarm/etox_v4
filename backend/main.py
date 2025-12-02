from fastapi import FastAPI
from fastapi.responses import JSONResponse
import json

app = FastAPI()

@app.get("/templates")
def get_templates():
    with open("/Users/upsomedia/Desktop/etos/backend/templates/templates.json", "r") as f:
        data = json.load(f)
    return JSONResponse(content=data)
