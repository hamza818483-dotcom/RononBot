FROM python:3.11-slim
WORKDIR /app

ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y \
    poppler-utils \
    fonts-noto \
    fonts-noto-color-emoji \
    chromium \
    && rm -rf /var/lib/apt/lists/*

ENV CHROMIUM_PATH=/usr/bin/chromium

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .
COPY mcq_handlers.py .
COPY sheet_handlers.py .
COPY main.py .
COPY RononBot_Command_Guide.md .

# Persistent storage now via Supabase (SUPABASE_URL/SUPABASE_KEY env vars) — sqlite is only an emergency fallback
ENV DB_PATH=/app/ronon.db

CMD ["python", "main.py"]
