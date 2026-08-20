# Backend Dockerfile for the FastAPI ASGI application
FROM python:3.11-slim

WORKDIR /app

RUN sed -i 's/deb.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || \
    sed -i 's/deb.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list 2>/dev/null || true

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt


# Copy application code
COPY . .

# .onto-runtime directory is volume-mounted at runtime; no local data/logs dirs needed

# Expose Consumer ASGI and standalone MCP ports
EXPOSE 5055 7488

# Set environment variables
ENV PYTHONUNBUFFERED=1

STOPSIGNAL SIGTERM

# Run the application
CMD ["uvicorn", "app:create_app", "--factory", "--host=0.0.0.0", "--port=5055", "--workers=1", "--timeout-graceful-shutdown=45", "--access-log"]
