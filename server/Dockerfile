# syntax=docker/dockerfile:1
# SPDX-License-Identifier: MIT
# Image du serveur LapiNou, équivalente au service systemd (docs/lapinou/service.md).
# Construite depuis la racine du dépôt (le serveur importe tools/app-server et lit
# userland/proto, R4) :
#   docker build -f server/Dockerfile -t lapinou .
# Les modèles ne sont PAS dans l'image : ils vont dans le volume /var/lib/lapinou (HOME du
# service), récupérés une fois (voir la doc), puis gardés d'une image à l'autre.

ARG PYTHON_IMAGE=python:3.12-slim-bookworm

# ---- construction : venv avec les dépendances figées (uv.lock) ----
FROM ${PYTHON_IMAGE} AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.8 /uv /usr/local/bin/uv
# speexaec se compile en 3.12 (libspeexdsp-dev, gcc ; les en-têtes Python sont dans l'image).
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc libc6-dev libspeexdsp-dev \
 && rm -rf /var/lib/apt/lists/*
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /opt/lapinou/server
COPY server/pyproject.toml server/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY server/ ./
COPY tools/app-server/ /opt/lapinou/tools/app-server/
COPY userland/proto/ /opt/lapinou/userland/proto/
RUN uv sync --frozen --no-dev

# ---- exécution : non root, sans compilateur ----
FROM ${PYTHON_IMAGE}
# protoc : codec du protocole plateforme ; ffmpeg : voix ; libspeexdsp1 : annulation d'écho.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg libspeexdsp1 protobuf-compiler \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --system --user-group --home-dir /var/lib/lapinou --shell /usr/sbin/nologin lapinou \
 && mkdir -p /etc/lapinou /var/lib/lapinou \
 && chown lapinou:lapinou /etc/lapinou /var/lib/lapinou
COPY --from=build /usr/local/bin/uv /usr/local/bin/uv
COPY --from=build /opt/lapinou /opt/lapinou
ENV HOME=/var/lib/lapinou \
    PATH=/var/lib/lapinou/.local/bin:/opt/lapinou/server/.venv/bin:/usr/local/bin:/usr/bin:/bin \
    PYTHONUNBUFFERED=1
WORKDIR /opt/lapinou/server
USER lapinou
VOLUME ["/etc/lapinou", "/var/lib/lapinou"]
EXPOSE 9123 9300 8080 8081 8443
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
  CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8080/api/health', timeout=4)"]
ENTRYPOINT ["python", "-m", "lapinou", "--config", "/etc/lapinou/config.toml", "--db", "/var/lib/lapinou/lapinou.db"]
