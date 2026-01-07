FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    pkg-config \
    libjpeg62-turbo-dev \
    zlib1g-dev \
    libfreetype6-dev \
    liblcms2-dev \
    libopenjp2-7-dev \
    libtiff5-dev \
    libwebp-dev \
    libharfbuzz-dev \
    libfribidi-dev \
    libraqm-dev \
 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN PIP_NO_BINARY=pillow pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PORT=8000
CMD sh -c "uvicorn backend.app:app --host 0.0.0.0 --port ${PORT:-8000}"
