FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY backend ./
ENV HUB_HOST=0.0.0.0 HUB_PORT=8200
VOLUME /data
EXPOSE 8200
CMD ["python", "run.py"]
