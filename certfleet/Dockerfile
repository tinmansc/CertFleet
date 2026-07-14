# ---------------------------------------------------------------------------
# Frontend build stage — compiles the React/Vite app to static files.
#
# Self-contained on purpose: a fresh git clone has no frontend/dist (it's
# gitignored), so building it here lets Home Assistant's Supervisor build the
# add-on locally straight from a source checkout with no pre-build step.
#
# NOTE (multi-arch CI): dist is arch-independent, so for a cross-arch build
# pin this stage to the native builder —
#   FROM --platform=$BUILDPLATFORM node:20-alpine AS frontend
# — to avoid running the JS build under slow QEMU emulation. Left unpinned
# here so single-arch local Supervisor builds work without requiring BuildKit.
# ---------------------------------------------------------------------------
# BUILD_FROM must be declared before the first FROM so it's in scope for the
# runtime stage's `FROM ${BUILD_FROM}` below (the Supervisor passes it per-arch
# from build.yaml; the default keeps a bare `docker build` working too).
ARG BUILD_FROM=python:3.12-alpine

FROM node:20-alpine AS frontend
WORKDIR /frontend
# package files first for layer-cache friendliness; node_modules/dist are
# kept out of the context by .dockerignore so this npm ci is authoritative.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ---------------------------------------------------------------------------
# Runtime stage
# ---------------------------------------------------------------------------
FROM ${BUILD_FROM}

# System deps for cryptography, paramiko, lxml
RUN apk add --no-cache \
    gcc musl-dev libffi-dev openssl-dev cargo \
    openssh-client openssl sshpass curl

WORKDIR /app

# Python backend
COPY backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Pre-built frontend static files from the stage above
COPY --from=frontend /frontend/dist /app/static

# Backend source
COPY backend /app/backend

# Bundled helper scripts (e.g. HPE 1950 deployer)
COPY scripts /app/scripts

COPY run.sh /run.sh
RUN chmod +x /run.sh

CMD ["/run.sh"]
