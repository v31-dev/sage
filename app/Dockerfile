FROM python:3.12-slim

ARG ENV=production

# Install dependencies (gdb: required by `memray attach` to inject into a live process)
RUN apt-get update && apt-get -y upgrade && apt-get install -y --no-install-recommends \
  curl \
  gdb \
  && rm -rf /var/lib/apt/lists/*

# Install Tailscale
RUN curl -fsSL https://tailscale.com/install.sh | sh

# Live-profiling tools (attach via `docker exec`; need SYS_PTRACE + gdb)
RUN pip install --no-cache-dir py-spy==0.4.2 memray==1.19.3

WORKDIR /app

COPY ./requirements.txt /app/requirements.txt
COPY ./requirements-dev.txt /app/requirements-dev.txt

RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir --upgrade -r /app/requirements.txt && \
    if [ "$ENV" = "development" ]; then pip install --no-cache-dir --upgrade -r /app/requirements-dev.txt; fi

COPY . /app

ENV PORT=9000
ENV METRICS_PORT=9001
ENV PYTHONUNBUFFERED=1
ENV MALLOC_ARENA_MAX=2

EXPOSE 9000
EXPOSE 9001

CMD ["python", "main.py"]