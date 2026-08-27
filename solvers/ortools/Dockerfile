FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ORTOOLS_SOLVER_STATE_DIR=/var/lib/ortools-solver \
    ORTOOLS_SOLVER_ENFORCE_OS_LIMITS=1

WORKDIR /opt/ortools-solver
COPY solvers/ortools/requirements.txt /opt/ortools-solver/solvers/ortools/requirements.txt
RUN pip install --no-cache-dir -r solvers/ortools/requirements.txt \
    && useradd --create-home --uid 10001 solver \
    && mkdir -p /var/lib/ortools-solver \
    && chown -R solver:solver /opt/ortools-solver /var/lib/ortools-solver
COPY contracts/solver_services/ /opt/ortools-solver/contracts/solver_services/
COPY solvers/__init__.py /opt/ortools-solver/solvers/__init__.py
COPY solvers/ortools/ /opt/ortools-solver/solvers/ortools/
RUN chown -R solver:solver /opt/ortools-solver

USER solver
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2)"
ENTRYPOINT ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "8", "solvers.ortools.app:create_app()"]
