from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import requests
import os

app = FastAPI()

# Allow your front-end to access the API
origins = ["*"]  # change "*" to your domain for security
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

OPENAI_KEY = os.environ.get("OPENAI_KEY")

class ImageRequest(BaseModel):
    prompt: str
    n: int = 1
    size: str = "512x512"

class TextRequest(BaseModel):
    prompt: str

@app.post("/aiImage")
def generate_ai_image(req: ImageRequest):
    if not req.prompt:
        return {"error": "Prompt required"}
    headers = {"Authorization": f"Bearer {OPENAI_KEY}"}
    data = {"prompt": req.prompt, "n": req.n, "size": req.size}
    r = requests.post("https://api.openai.com/v1/images/generations", headers=headers, json=data)
    res = r.json()
    urls = [img["url"] for img in res.get("data", [])]
    return {"urls": urls}

@app.post("/aiText")
def generate_ai_text(req: TextRequest):
    if not req.prompt:
        return {"error": "Prompt required"}
    headers = {"Authorization": f"Bearer {OPENAI_KEY}"}
    data = {
        "model": "gpt-3.5-turbo",
        "messages": [{"role": "user", "content": req.prompt}]
    }
    r = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=data)
    res = r.json()
    text = res.get("choices", [{}])[0].get("message", {}).get("content", "")
    return {"text": text}
