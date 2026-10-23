FROM node:24-bookworm-slim AS frontend
WORKDIR /build
COPY frontend/package*.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home evaluation \
    && mkdir -p /var/lib/evaluation && chown evaluation /var/lib/evaluation
COPY evaluation/ ./evaluation/
COPY run.py ./
COPY --from=frontend /build/dist ./frontend/dist
USER evaluation
ENV EVAL_DATA_DIR=/var/lib/evaluation
EXPOSE 8090
CMD ["python", "run.py"]
