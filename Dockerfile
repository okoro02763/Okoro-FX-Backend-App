FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# TA-Lib 0.6.x and psycopg2-binary ship manylinux wheels, so no C build is
# strictly required; build-essential is kept for safety on rare wheel misses.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# daphne serves HTTP *and* WebSockets (backend.asgi:application).
CMD ["daphne", "-b", "0.0.0.0", "-p", "8000", "backend.asgi:application"]
