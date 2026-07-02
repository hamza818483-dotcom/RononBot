FROM python:3.11-slim

# poppler-utils needed for pdf2image
RUN apt-get update && apt-get install -y --no-install-recommends \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

# Persistent storage now via Supabase (SUPABASE_URL/SUPABASE_KEY env vars) — sqlite is only an emergency fallback
ENV DB_PATH=/app/ronon.db

CMD ["python", "bot.py"]
