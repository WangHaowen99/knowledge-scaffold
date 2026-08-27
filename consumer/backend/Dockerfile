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

# The Consumer image is built from the repository root so the public solver
# service contract can be packaged without copying the Solver Service source.
# Keep requirements in its own layer for better build caching.
COPY consumer/backend/requirements.txt ./requirements.txt

# Install Python dependencies
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt


# Copy only the Consumer application.  The Consumer-owned Connector lives in
# this source tree, while the independent Solver Service stays outside it.
COPY consumer/backend/ ./
# The OR-Tools service DTO contract is a standalone runtime dependency. It is
# copied from the repository root while the Solver Service implementation
# remains outside the Consumer image.
COPY contracts/solver_services/ /app/contracts/solver_services/

# Bind authoritative results/checkpoints to the exact shipped source,
# installed Python distributions and interpreter runtime.
RUN python agent/direct_execution/build_revision.py \
    --root /app \
    --output /app/.app-runtime-revision

# .onto-runtime directory is volume-mounted at runtime; no local data/logs dirs needed

# Expose Consumer ASGI and standalone MCP ports
EXPOSE 5055 7488

# Set environment variables
ENV PYTHONUNBUFFERED=1

STOPSIGNAL SIGTERM

# Run the application
CMD ["uvicorn", "app:create_app", "--factory", "--host=0.0.0.0", "--port=5055", "--workers=1", "--timeout-graceful-shutdown=45", "--access-log"]
