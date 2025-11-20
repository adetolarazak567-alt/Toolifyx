from fastapi import FastAPI
from pydantic import BaseModel
from openai import OpenAI
import os

app = FastAPI()
client = OpenAI(api_key=os.getenv("OPENAI_KEY"))

# ----- REQUEST MODELS -----
class TextRequest(BaseModel):
    prompt: str

class ImageRequest(BaseModel):
    prompt: str
    count: int = 1
    size: str = "512x512"

# ----- AI TEXT -----
@app.post("/api/text")
async def generate_text(req: TextRequest):
    try:
        response = client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[{"role":"user","content": req.prompt}]
        )
        return {"text": response.choices[0].message.content}
    except Exception as e:
        return {"error": str(e)}

# ----- AI IMAGE -----
@app.post("/api/image")
async def generate_image(req: ImageRequest):
    try:
        response = client.images.generate(
            model="gpt-image-1",
            prompt=req.prompt,
            n=req.count,
            size=req.size
        )
        return {"images": [img.url for img in response.data]}
    except Exception as e:
        return {"error": str(e)}

# ----- HEALTH CHECK -----
@app.get("/")
async def root():
    return {"status": "OK"}
