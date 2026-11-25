FROM python:3.11-slim

FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app

ENV PORT=8000
CMD ["python", "app.py"]

WORKDIR /app
COPY . /app

EXPOSE 8000
CMD ["python", "app.py"]