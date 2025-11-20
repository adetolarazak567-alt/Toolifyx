// server.js
import express from "express";
import cors from "cors";
import OpenAI from "openai";

const app = express();
app.use(cors());
app.use(express.json());

const openai = new OpenAI({
  apiKey: process.env.OPENAI_API_KEY,
});

// ---------------- AI Text Generation ----------------
app.post("/api/text", async (req, res) => {
  try {
    const { prompt } = req.body;
    if (!prompt) return res.status(400).json({ error: "No prompt provided" });

    const response = await openai.chat.completions.create({
      model: "gpt-4o-mini",
      messages: [{ role: "user", content: prompt }],
      temperature: 0.7,
      max_tokens: 500,
    });

    const text = response.choices?.[0]?.message?.content || "";
    res.json({ text });
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: "Connection failed" });
  }
});

// ---------------- AI Image Generation ----------------
app.post("/api/image", async (req, res) => {
  try {
    const { prompt, count = 1, size = "512x512" } = req.body;
    if (!prompt) return res.status(400).json({ error: "No prompt provided" });

    const response = await openai.images.generate({
      model: "gpt-image-1",
      prompt,
      size,
      n: count,
    });

    const images = response.data.map((img) => img.url);
    res.json({ images });
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: "Connection failed" });
  }
});

// ---------------- Start Server ----------------
const PORT = process.env.PORT || 5000;
app.listen(PORT, () => console.log(`Server running on port ${PORT}`));
