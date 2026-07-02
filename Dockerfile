FROM python:3.11-slim

# poppler-utils needed for pdf2image
RUN apt-get update && apt-get install -y --no-install-recommends \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

# Render persists disk only on paid plans; sqlite file lives in /app (fine for free tier restart-loss)
ENV DB_PATH=/app/ronon.db

CMD ["python", "bot.py"]
