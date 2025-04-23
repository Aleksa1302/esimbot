FROM python:3.11-slim

WORKDIR /app

COPY . /app

RUN pip install --no-cache-dir python-telegram-bot==20.3 pandas requests qrcode[pil]

CMD ["python", "bot.py"]
