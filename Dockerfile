# syntax=docker/dockerfile:1
# Etapa de construcción: las dependencias se resuelven una vez y no viajan
# con las herramientas de compilación a la imagen final.
FROM python:3.12-slim AS build

WORKDIR /build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1

COPY pyproject.toml ./
COPY src ./src
RUN python -m venv /opt/venv && /opt/venv/bin/pip install --upgrade pip && /opt/venv/bin/pip install .

# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LUISCV_ENVIRONMENT=prod

# Usuario sin privilegios: el proceso no necesita root y no debe tenerlo.
RUN useradd --system --uid 10001 --create-home agente
COPY --from=build /opt/venv /opt/venv

WORKDIR /app
USER agente
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2).status==200 else 1)"

# Sin `--reload`, un solo proceso por tarea: la escala la da ECS, no el WSGI.
# `timeout-keep-alive` por debajo del idle_timeout del ALB (120 s).
CMD ["uvicorn", "luis_cv.main:app", "--host", "0.0.0.0", "--port", "8080", \
     "--timeout-keep-alive", "115", "--no-access-log"]
