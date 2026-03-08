FROM python:3.12-slim

# System deps: ffmpeg + yt-dlp runtime requirements
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install yt-dlp binary
RUN curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp \
    -o /usr/local/bin/yt-dlp \
    && chmod +x /usr/local/bin/yt-dlp

WORKDIR /app

# Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY backend/ ./backend/
COPY frontend/ ./frontend/

# Mount points for volumes
RUN mkdir -p /downloads/audio /downloads/video /config

ENV CONFIG_PATH=/config/settings.yaml
ENV YT_DLP_BIN=/usr/local/bin/yt-dlp
ENV FFMPEG_BIN=ffmpeg
ENV PYTHONPATH=/app

EXPOSE 8000

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
