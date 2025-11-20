# app.py
from fastapi import FastAPI
from pydantic import BaseModel
from openai import OpenAI
import os

app = FastAPI()

# Initialize OpenAI client using the environment variable
client = OpenAI(api_key=os.getenv("OPENAI_KEY"))

class TextRequest(BaseModel):
    prompt: str

@app.post("/generate-image")
async def generate_image(req: TextRequest):
    """
    Generate an image from a text prompt using OpenAI
    """
    try:
        response = client.images.generate(
            model="gpt-image-1",
            prompt=req.prompt,
            size="1024x1024"
        )
        # Return the first generated image URL
        return {"image_url": response.data[0].url}
    except Exception as e:
        return {"error": str(e)}

@app.get("/")
async def root():
    return {"status": "OK"}
