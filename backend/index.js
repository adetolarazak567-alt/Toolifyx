import express from "express";
import cors from "cors";
import dotenv from "dotenv";
import OpenAI from "openai";

dotenv.config();

const app = express();
const port = process.env.PORT || 3000;

app.use(cors());
app.use(express.json());

const openai = new OpenAI({
  apiKey: process.env.OPENAI_API_KEY
});

// ---------------- AI TEXT GENERATOR ----------------
app.post("/api/text", async (req, res) => {
  try {
    const { prompt } = req.body;
    if (!prompt) return res.status(400).json({ error: "Prompt is required" });

    const response = await openai.chat.completions.create({
      model: "gpt-4o-mini",
      messages: [{ role: "user", content: prompt }],
      temperature: 0.7
    });

    const text = response.choices[0].message.content;
    res.json({ text });
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: "AI Text generation failed" });
  }
});

// ---------------- AI IMAGE GENERATOR ----------------
app.post("/api/image", async (req, res) => {
  try {
    const { prompt, count = 1, size = "512x512" } = req.body;
    if (!prompt) return res.status(400).json({ error: "Prompt is required" });

    const response = await openai.images.generate({
      model: "gpt-image-1",
      prompt,
      size,
      n: count
    });

    const images = response.data.map(img => img.url);
    res.json({ images });
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: "AI Image generation failed" });
  }
});

// ---------------- SERVER ----------------
app.get("/", (req, res) => {
  res.send("ToolifyX Backend is running");
});

app.listen(port, () => {
  console.log(`Server running on port ${port}`);
});
