FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY auth_server ./auth_server
COPY run.py ./

# 容器内监听所有接口；对外不暴露端口由编排层（compose 不 publish）保证
ENV AUTH_SERVER_HOST=0.0.0.0 \
    AUTH_SERVER_PORT=5056 \
    AUTH_SERVER_DATA_DIR=/data

VOLUME /data
EXPOSE 5056

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5056/health', timeout=2).status == 200 else 1)"]

CMD ["python", "run.py"]
