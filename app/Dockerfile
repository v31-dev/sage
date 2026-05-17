FROM python:3.12.3-slim

ARG ENV=production

# Install dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
  curl rsync openssh-client \
  && rm -rf /var/lib/apt/lists/*

# Install Tailscale 
RUN curl -fsSL https://tailscale.com/install.sh | sh

WORKDIR /app

COPY ./requirements.txt /app/requirements.txt
COPY ./requirements-dev.txt /app/requirements-dev.txt

RUN pip install --no-cache-dir --upgrade -r /app/requirements.txt && \
    if [ "$ENV" = "development" ]; then pip install --no-cache-dir --upgrade -r /app/requirements-dev.txt; fi

COPY . /app

ENV PORT=9000
ENV METRICS_PORT=9001
ENV PYTHONUNBUFFERED=1

EXPOSE 9000
EXPOSE 9001

CMD ["python", "main.py"]