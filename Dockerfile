FROM python:3.11-slim

WORKDIR /app

# Copy code
COPY . .

# Install runtime dependencies in one go
RUN pip install --no-cache-dir \
        python-telegram-bot==20.3 \
        pandas \
        requests \
        qrcode[pil] \
        pycountry-convert

# Start the bot
CMD ["python", "bot.py"]
