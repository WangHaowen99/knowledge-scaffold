# Backend Dockerfile for Flask application
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

# Bind authoritative results/checkpoints to the exact shipped source,
# installed Python distributions and interpreter runtime.
RUN python agent/direct_execution/build_revision.py \
    --root /app \
    --output /app/.app-runtime-revision

# .onto-runtime directory is volume-mounted at runtime; no local data/logs dirs needed

# Expose Flask and standalone MCP ports
EXPOSE 5055 7488

# Set environment variables
ENV FLASK_APP=run.py
ENV PYTHONUNBUFFERED=1

# Run the application
CMD ["gunicorn", "wsgi:app", "--worker-class=gthread", "--threads=64", "--bind=0.0.0.0:5055", "--timeout=1800", "--access-logfile=-", "--error-logfile=-"]
