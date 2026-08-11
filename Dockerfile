# Production image for ConveyerSeven edge controller.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# OpenCV/pywebview runtime libraries; hardware devices are supplied by Compose.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 libgl1 libgomp1 libusb-1.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . ./
RUN useradd --create-home --uid 10001 conveyer \
    && chown -R conveyer:conveyer /app
USER conveyer

VOLUME ["/app/data", "/app/logs", "/app/archive"]

CMD ["python", "main.py"]
