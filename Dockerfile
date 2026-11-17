FROM python:3.12-slim

# tzdata — чтобы Europe/Moscow работал внутри slim-образа
RUN pip install --no-cache-dir tzdata

WORKDIR /app
COPY bot.py .

ENV PYTHONUNBUFFERED=1 \
    TZ_NAME=Europe/Moscow \
    STATE_FILE=/data/state.json

# /data — volume: привязка чата и отметка «уведомление отправлено» переживают обновления образа
VOLUME ["/data"]

# не от root
RUN useradd --create-home --uid 1000 bot && mkdir -p /data && chown bot:bot /data
USER bot

CMD ["python", "bot.py"]
