FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    BOT_DATA_DIR=/app/data

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

VOLUME ["/app/data"]

CMD ["python", "bot.py"]
