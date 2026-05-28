# Stage 1: Build UI
FROM node:lts-alpine AS ui-builder

WORKDIR /ui

ARG VITE_LOAD_DELAY=0
ENV VITE_LOAD_DELAY=${VITE_LOAD_DELAY}

COPY ui/package*.json ./

RUN npm install

COPY ui/ .

RUN npm run build

# Stage 2: Python app with built UI
FROM python:3.12.3-slim

# Install dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
  curl rsync openssh-client \
  && rm -rf /var/lib/apt/lists/*

# Install Tailscale
RUN curl -fsSL https://tailscale.com/install.sh | sh

WORKDIR /app

# Copy Python requirements
COPY app/requirements.txt /app/requirements.txt

# Install Python dependencies
RUN pip install --no-cache-dir --upgrade -r /app/requirements.txt

# Copy Python app
COPY app/ /app/

# Copy VERSION last so the root release version overwrites the app placeholder
COPY VERSION /app/VERSION
RUN test -f /app/VERSION || (echo "ERROR: VERSION file is required" && exit 1)

# Copy built UI static files to app for serving
COPY --from=ui-builder /ui/dist /app/static

ENV PORT=9000
ENV METRICS_PORT=9001
ENV PYTHONUNBUFFERED=1

EXPOSE 9000
EXPOSE 9001

CMD ["python", "main.py"]
