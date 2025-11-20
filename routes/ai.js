const express = require('express');
const router = express.Router();
const OpenAI = require('openai');

const openai = new OpenAI({
  apiKey: process.env.OPENAI_API_KEY
});

// AI Text
router.post('/text', async (req, res) => {
  try {
    const { prompt } = req.body;
    const response = await openai.chat.completions.create({
      model: "gpt-4o-mini",
      messages: [{ role: "user", content: prompt }],
      temperature: 0.7
    });
    res.json({ text: response.choices[0].message.content });
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: 'Failed to generate text' });
  }
});

// AI Image
router.post('/image', async (req, res) => {
  try {
    const { prompt, count, size } = req.body;
    const response = await openai.images.generate({
      model: "gpt-image-1",
      prompt: prompt,
      size: size || "512x512",
      n: count || 1
    });
    const images = response.data.map(img => img.url);
    res.json({ images });
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: 'Failed to generate images' });
  }
});

module.exports = rimages
