# Use official Python slim image
FROM python:3.13-slim

# Install system dependencies including FFmpeg
RUN apt-get update && apt-get install -y \
    ffmpeg \
    build-essential \
    libffi-dev \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements first (caching)
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of your project
COPY . .

# Expose the port Render will use
EXPOSE 10000

# Set environment variable for Flask uploads
ENV MAX_CONTENT_LENGTH=5368709120  # 5GB

# Start the app using Gunicorn
CMD ["gunicorn", "--bind", "0.0.0.0:10000", "app:app", "--workers=1", "--threads=4", "--timeout=600"]
