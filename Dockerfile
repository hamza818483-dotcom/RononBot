FROM python:3.11-slim

# poppler-utils needed for pdf2image
# chromium + fonts-noto-bengali needed for /sheet PDF generation (Bengali script render)
RUN apt-get update && apt-get install -y --no-install-recommends \
    poppler-utils \
    fonts-noto \
    fonts-noto-bengali \
    fonts-noto-color-emoji \
    ca-certificates \
    && apt-get install -y chromium || apt-get install -y chromium-browser \
    && rm -rf /var/lib/apt/lists/* \
    && fc-cache -f

ENV CHROMIUM_PATH=/usr/bin/chromium
RUN [ -x /usr/bin/chromium ] || ln -sf /usr/bin/chromium-browser /usr/bin/chromium

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

# Persistent storage now via Supabase (SUPABASE_URL/SUPABASE_KEY env vars) — sqlite is only an emergency fallback
ENV DB_PATH=/app/ronon.db

CMD ["python", "bot.py"]
