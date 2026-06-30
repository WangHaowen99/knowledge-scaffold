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

# Copy requirements first for better caching. This Dockerfile expects the
# repository root as build context.
COPY platform/server/requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt


# Copy application code and package fragments.
COPY platform/server/ ./
COPY platform/ ./platform/
COPY runtime/ ./runtime/
COPY modeling/skills/ ./modeling/skills/

# Create necessary directories
RUN mkdir -p data logs

# Expose the port the app runs on
EXPOSE 5055

# Set environment variables
ENV FLASK_APP=run.py
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app/platform/server:/app/modeling/skills

# Run the application
CMD ["gunicorn", "wsgi:app", "--worker-class=gthread", "--threads=64", "--bind=0.0.0.0:5055", "--timeout=1800", "--access-logfile=-", "--error-logfile=-"]
