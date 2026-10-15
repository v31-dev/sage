ARG TAILSCALE_VERSION=v1.102.5

FROM tailscale/tailscale:${TAILSCALE_VERSION} AS tailscale

FROM python:3.12-slim

ARG ENV=production

# Development only: gdb, which `memray attach` needs to inject into a live process. The
# profilers themselves (py-spy, memray) are in requirements-dev.txt; see docs/profiling.md.
RUN apt-get update && apt-get -y upgrade && \
    if [ "$ENV" = "development" ]; then apt-get install -y --no-install-recommends gdb; fi && \
    rm -rf /var/lib/apt/lists/*

COPY --from=tailscale /usr/local/bin/tailscale /usr/local/bin/tailscaled /usr/local/bin/

WORKDIR /app

COPY ./requirements.txt /app/requirements.txt
COPY ./requirements-dev.txt /app/requirements-dev.txt

RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir --upgrade -r /app/requirements.txt && \
    if [ "$ENV" = "development" ]; then pip install --no-cache-dir --upgrade -r /app/requirements-dev.txt; fi

COPY . /app

ENV PORT=80
ENV METRICS_PORT=9001
ENV PYTHONUNBUFFERED=1
ENV MALLOC_ARENA_MAX=2

EXPOSE 80
EXPOSE 443
EXPOSE 9001

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python", "main.py"]