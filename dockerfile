FROM python:3.11-slim

WORKDIR /app

# Install system deps (for Pillow)
RUN apt-get update && apt-get install -y build-essential libjpeg-dev zlib1g-dev libfreetype6-dev libpng-dev && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml poetry.lock* /app/  # or requirements.txt
# If you use requirements.txt:
# COPY requirements.txt /app/
# RUN pip install -r requirements.txt

# Install pip dependencies (adjust depending on your project)
RUN python -m pip install --upgrade pip
# If you have requirements.txt present:
# COPY requirements.txt /app/
# RUN pip install -r requirements.txt

# Fallback install common deps used by the patch
RUN pip install fastapi uvicorn gunicorn pillow cachetools google-auth redis

# Copy app
COPY . /app

# Expose port
EXPOSE 8000

# Use Gunicorn with Uvicorn workers
CMD ["gunicorn", "-k", "uvicorn.workers.UvicornWorker", "app:app", "-w", "2", "-b", "0.0.0.0:8000", "--timeout", "120"]