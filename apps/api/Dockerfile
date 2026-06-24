FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY pipeline /app/pipeline
COPY apps/api/app /app/app
COPY scripts /app/scripts

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

