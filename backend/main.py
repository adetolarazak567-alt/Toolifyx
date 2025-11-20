from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from openai import OpenAI
import os

app = FastAPI()

# CORS for frontend access
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# OpenAI Client (latest SDK)
client = OpenAI(api_key=os.getenv("OPENAI_KEY"))


# ================================ MODELS ================================

class TextRequest(BaseModel):
    prompt: str

class ImageRequest(BaseModel):
    prompt: str
    n: int = 1
    size: str = "1024x1024"

class TextToImageRequest(BaseModel):
    description: str
    size: str = "1024x1024"


# ================================ TEXT GENERATION ================================

@app.post("/api/text")
async def generate_text(req: TextRequest):
    if not req.prompt:
        return {"error": "Prompt is required"}

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": req.prompt}]
    )

    text = response.choices[0].message["content"]
    return {"text": text}


# ================================ IMAGE GENERATION ================================

@app.post("/api/image")
async def generate_image(req: ImageRequest):
    if not req.prompt:
        return {"error": "Prompt is required"}

    response = client.images.generate(
        model="gpt-image-1",
        prompt=req.prompt,
        size=req.size,
        n=req.n
    )

    urls = [img.url for img in response.data]
    return {"urls": urls}


# ================================ TEXT → IMAGE ================================
@app.post("/api/text2image")
async def text_to_image(req: TextToImageRequest):
    if not req.description:
        return {"error": "Description is required"}

    # Step 1 — Turn text into an AI-generated prompt
    prompt_response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "user",
                "content":
                f"Convert this into a detailed, vivid image-generation prompt:\n\n{req.description}"
            }
        ]
    )

    improved_prompt = prompt_response.choices[0].message["content"]

    # Step 2 — Generate image from the improved prompt
    image_response = client.images.generate(
        model="gpt-image-1",
        prompt=improved_prompt,
        size=req.size,
        n=1
    )

    image_url = image_response.data[0].url

    return {
        "prompt_used": improved_prompt,
        "image_url": image_url
    }
