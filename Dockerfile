ARG TAILSCALE_VERSION=v1.102.5

FROM tailscale/tailscale:${TAILSCALE_VERSION} AS tailscale

# Stage 1: Build UI, on the Node major in ui/.nvmrc
FROM node:24-alpine AS ui-builder

WORKDIR /ui

ARG VITE_LOAD_DELAY=0
ENV VITE_LOAD_DELAY=${VITE_LOAD_DELAY}

COPY ui/package*.json ./

RUN npm install

COPY ui/ .

RUN npm run build

# Stage 2: Python app with built UI
FROM python:3.12-slim

# Security updates for the base image's packages. Live-profiling tools are dev-only
# (app/Dockerfile).
RUN apt-get update && apt-get -y upgrade && rm -rf /var/lib/apt/lists/*

COPY --from=tailscale /usr/local/bin/tailscale /usr/local/bin/tailscaled /usr/local/bin/

WORKDIR /app

# Copy Python requirements
COPY app/requirements.txt /app/requirements.txt

# Install Python dependencies
RUN pip install --no-cache-dir --upgrade pip setuptools wheel \
  && pip install --no-cache-dir --upgrade -r /app/requirements.txt

# Copy Python app
COPY app/ /app/

# Copy VERSION last so the root release version overwrites the app placeholder
COPY VERSION /app/VERSION
RUN test -f /app/VERSION || (echo "ERROR: VERSION file is required" && exit 1)

# Copy built UI static files to app for serving
COPY --from=ui-builder /ui/dist /app/static

ENV PORT=80
ENV METRICS_PORT=9001
ENV PYTHONUNBUFFERED=1
ENV MALLOC_ARENA_MAX=2

EXPOSE 80
EXPOSE 443
EXPOSE 9001

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python", "main.py"]
