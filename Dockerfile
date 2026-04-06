FROM python:3.13-slim

RUN apt-get update && \
  apt-get install -y --no-install-recommends ffmpeg yt-dlp && \
  rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv && \
  uv sync --frozen --no-dev && \
  rm -rf ~/.cache/uv

COPY src/ src/

RUN mkdir -p downloads downloads_tmp

ENV MACH_MUKKE_API_KEY="secret"

EXPOSE 8000

CMD ["uv", "run", "src/mach_mukke/server.py"]
